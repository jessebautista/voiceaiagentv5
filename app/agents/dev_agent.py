import logging
import os
import json
from pathlib import Path
from typing import TypedDict, Annotated, Sequence, Any
import operator

from langgraph.graph import StateGraph, END
from anthropic import AsyncAnthropic
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

logger = logging.getLogger(__name__)

class AgentState(TypedDict):
    plan: str
    instruction: str
    repo_path: str
    review_feedback: str

async def run_dev_agent(repo_path: str, instruction: str) -> str:
    """
    Initializes a LangGraph Multi-Agent pipeline (Planner -> Coder -> Reviewer)
    to execute the given instruction, powered by Claude 3.5 Sonnet and MCP.
    """
    logger.info(f"Running Multi-Agent Dev Pipeline on {repo_path} with instruction: {instruction}")
    
    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", repo_path],
    )
    
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_response = await session.list_tools()
            
            anthropic_tools = [{
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.inputSchema
            } for tool in mcp_response.tools]
            
            anthropic_tools.append({
                "name": "get_git_diff",
                "description": "Gets the current git diff of all modified files in the repository.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            })
            
            client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

            async def call_claude_with_tools(system_prompt: str, user_prompt: str) -> str:
                messages = [{"role": "user", "content": user_prompt}]
                
                while True:
                    response = await client.messages.create(
                        model="claude-sonnet-4-5-20250929",
                        max_tokens=4096,
                        system=system_prompt,
                        messages=messages,
                        tools=anthropic_tools
                    )
                    
                    # Convert response content to primitive dict list
                    assistant_content = []
                    for block in response.content:
                        if block.type == "text":
                            assistant_content.append({"type": "text", "text": block.text})
                        elif block.type == "tool_use":
                            assistant_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input
                            })
                    
                    messages.append({"role": "assistant", "content": assistant_content})
                    
                    if response.stop_reason == "tool_use":
                        tool_results = []
                        for block in response.content:
                            if block.type == "tool_use":
                                tool_name = block.name
                                tool_args = block.input
                                tool_id = block.id
                                
                                try:
                                    logger.warning(f"Calling tool {tool_name} with args {tool_args}")
                                    
                                    if tool_name == "get_git_diff":
                                        import subprocess
                                        diff_result = subprocess.run(
                                            "git diff HEAD",
                                            cwd=repo_path,
                                            shell=True,
                                            capture_output=True,
                                            text=True,
                                            encoding="utf-8"
                                        )
                                        result_text = diff_result.stdout
                                        if not result_text.strip():
                                            result_text = "No changes detected."
                                    else:
                                        result = await session.call_tool(tool_name, arguments=tool_args)
                                        
                                        result_text = ""
                                        if hasattr(result, "content"):
                                            for item in result.content:
                                                if item.type == "text":
                                                    result_text += item.text
                                        else:
                                            result_text = str(result)
                                        
                                    if len(result_text) > 15000:
                                        result_text = result_text[:15000] + "\n\n...[TRUNCATED]"
                                        
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": result_text
                                    })
                                except Exception as e:
                                    tool_results.append({
                                        "type": "tool_result",
                                        "tool_use_id": tool_id,
                                        "content": f"Error: {str(e)}",
                                        "is_error": True
                                    })
                        
                        messages.append({"role": "user", "content": tool_results})
                    else:
                        break
                        
                final_text = ""
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                return final_text

            # --- NODE: PLANNER ---
            async def planner_node(state: AgentState):
                logger.warning("Planner Node: Gathering context to build a plan.")
                system_prompt = f"""You are a Senior Software Architect working on a codebase at: {state["repo_path"]}
Your goal is to figure out EXACTLY how to implement the user's request. Do NOT write the final code yourself.
Use the tools provided to explore the filesystem, read files, and search text. Tools natively allow multiple arguments.
Analyze the user's request, gather context, then output a final detailed plan.
YOUR FINAL RESPONSE MUST BE A DETAILED MARKDOWN PLAN.
The plan must list exact absolute filepaths to modify and detailed logic for the junior developer."""
                
                user_prompt = f"User Request: {state['instruction']}"
                plan = await call_claude_with_tools(system_prompt, user_prompt)
                
                logger.warning(f"Planner generated plan:\n{plan[:300]}...\n")
                return {"plan": plan}

            # --- NODE: CODER ---
            async def coder_node(state: AgentState):
                logger.warning("Coder Node: Executing the plan.")
                system_prompt = f"""You are a Junior Software Developer working on a codebase at: {state["repo_path"]}
You are given a plan by the Architect, and your job is to execute it EXACTLY as instructed.
You MUST use the `write_file` tool to make code changes on the disk. NEVER hallucinate making changes.
Read the file first if you need to, then call `write_file` with the full modified content. Make sure to pass the absolute complete filepath and complete file contents. Do not truncate files.
When you are completely finished writing the files, just say 'I have completed the code changes.'"""
                
                user_prompt = f"Architect's Plan:\n{state['plan']}\n\nPrevious Reviewer Feedback:\n{state.get('review_feedback', 'None. This is your first attempt.')}"
                coder_response = await call_claude_with_tools(system_prompt, user_prompt)
                logger.warning(f"Coder response: {coder_response[:500]}")
                return {}

            # --- NODE: REVIEWER ---
            async def reviewer_node(state: AgentState):
                logger.warning("Reviewer Node: Inspecting the codebase.")
                system_prompt = f"""You are a Tech Lead Code Reviewer for the codebase at: {state["repo_path"]}
A junior dev just finished changing code based on this User Request: {state["instruction"]}

You must use `get_git_diff` tool to see exactly what they changed.
Review their code for logical correctness, bugs, and if they fulfilled the user's request.
(Ignore minor accessibility Svelte warnings).

If the code looks perfect and solves the user request, your FINAL OUTPUT MUST explicitly contain the exact word "APPROVED".
If the code is wrong, incomplete, or if NO CHANGES WERE MADE, your FINAL OUTPUT MUST explicitly contain the exact word "REJECTED" followed by clear feedback for the junior dev to fix it."""
                
                user_prompt = "Review the current state of the repository. Are the changes APPROVED or REJECTED?"
                feedback = await call_claude_with_tools(system_prompt, user_prompt)
                logger.warning(f"Reviewer decision: {feedback[:500]}")
                
                return {"review_feedback": feedback}

            # --- ROUTING LOGIC ---
            def reviewer_route(state: AgentState):
                feedback = state.get("review_feedback", "")
                if "APPROVED" in feedback.upper():
                    logger.warning("Reviewer APPROVED the changes. Wrapping up.")
                    return END
                else:
                    logger.warning("Reviewer REJECTED the changes. Sending back to Coder.")
                    return "coder"

            # --- BUILD GRAPH ---
            workflow = StateGraph(AgentState)
            workflow.add_node("planner", planner_node)
            workflow.add_node("coder", coder_node)
            workflow.add_node("reviewer", reviewer_node)
            
            workflow.set_entry_point("planner")
            workflow.add_edge("planner", "coder")
            workflow.add_edge("coder", "reviewer")
            workflow.add_conditional_edges("reviewer", reviewer_route)
            
            app = workflow.compile()
            
            try:
                final_state = await app.ainvoke({
                    "instruction": instruction,
                    "repo_path": repo_path,
                    "plan": "",
                    "review_feedback": ""
                })
                return f"Development completed successfully!\n\nFinal Review Feedback:\n{final_state.get('review_feedback', 'Approved.')}"
            except Exception as e:
                logger.error(f"DevAgent execution failed: {e}")
                raise
