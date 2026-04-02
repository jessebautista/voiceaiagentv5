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
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_DEV_MODEL = "claude-sonnet-4-5-20250929"


def _load_dev_fix_rules() -> str:
    """Load strict dev-fix rules from app/prompts for planner/coder/reviewer prompts."""
    path = PROMPTS_DIR / "dev_fix_strict_rules.txt"
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("Failed to load dev-fix rules prompt: %s", e)
    return ""

class AgentState(TypedDict, total=False):
    plan: str
    instruction: str
    repo_path: str
    review_feedback: str
    review_cycle: int


def _get_env_model(var_name: str, fallback: str) -> str:
    value = (os.getenv(var_name) or "").strip()
    return value or fallback


def _should_escalate_model(instruction: str, state: AgentState) -> bool:
    """
    Escalate for retry-heavy contexts:
    - workflow retry path (instruction includes previous verification failure)
    - reviewer/coder loop beyond first cycle
    """
    if "Previous attempt failed verification" in instruction:
        return True
    if (state.get("review_cycle") or 0) >= 1:
        return True
    return False

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
            strict_rules = _load_dev_fix_rules()
            default_model = _get_env_model("DEV_AGENT_MODEL_DEFAULT", DEFAULT_DEV_MODEL)
            planner_model = _get_env_model("DEV_AGENT_MODEL_PLANNER", default_model)
            escalated_model = _get_env_model("DEV_AGENT_MODEL_ESCALATED", default_model)
            escalate_on_retry = (os.getenv("DEV_AGENT_MODEL_ESCALATE_ON_RETRY", "1").strip().lower() in ("1", "true", "yes"))
            logger.info(
                "Dev agent model routing | planner=%s default=%s escalated=%s escalate_on_retry=%s",
                planner_model,
                default_model,
                escalated_model,
                escalate_on_retry,
            )

            def choose_model_for_node(node_name: str, state: AgentState) -> str:
                if node_name == "planner":
                    return planner_model
                if node_name in ("coder", "reviewer") and escalate_on_retry and _should_escalate_model(instruction, state):
                    return escalated_model
                return default_model

            async def call_claude_with_tools(system_prompt: str, user_prompt: str, model_name: str, node_name: str) -> str:
                messages = [{"role": "user", "content": user_prompt}]
                tool_round = 0

                while True:
                    response = await client.messages.create(
                        model=model_name,
                        max_tokens=4096,
                        system=system_prompt,
                        messages=messages,
                        tools=anthropic_tools
                    )
                    # Log so user sees progress and knows the flow is not stuck
                    if response.stop_reason == "tool_use":
                        tool_round += 1
                        tool_names = [b.name for b in response.content if getattr(b, "type", None) == "tool_use"]
                        logger.info(
                            "Dev agent %s round %d (%s): calling %s",
                            node_name,
                            tool_round,
                            model_name,
                            ", ".join(tool_names),
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
                                    logger.debug("Calling tool %s with args %s", tool_name, tool_args)
                                    
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
                        logger.info("Dev agent: waiting for Claude response (round %d done)...", tool_round)
                    else:
                        break
                        
                final_text = ""
                for block in response.content:
                    if block.type == "text":
                        final_text += block.text
                return final_text

            # --- NODE: PLANNER ---
            async def planner_node(state: AgentState):
                logger.info("Planner Node: Gathering context to build a plan.")
                model_name = choose_model_for_node("planner", state)
                system_prompt = f"""You are a Senior Software Architect working on a codebase at: {state["repo_path"]}
Your goal is to figure out EXACTLY how to implement the user's request. Do NOT write the final code yourself.
Use the tools provided to explore the filesystem, read files, and search text. Tools natively allow multiple arguments.
Analyze the user's request, gather context, then output a final detailed plan.
YOUR FINAL RESPONSE MUST BE A DETAILED MARKDOWN PLAN.
The plan must list exact absolute filepaths to modify and detailed logic for the junior developer."""
                if strict_rules:
                    system_prompt += "\n\nStrict Dev Fix Rules:\n" + strict_rules
                
                user_prompt = f"User Request: {state['instruction']}"
                plan = await call_claude_with_tools(system_prompt, user_prompt, model_name, "planner")
                
                logger.info("Planner generated plan (first 300 chars): %s...", (plan[:300] or "").strip())
                return {"plan": plan}

            # --- NODE: CODER ---
            MAX_REVIEW_CYCLES = 2  # cap coder-reviewer loops to avoid runaway

            async def coder_node(state: AgentState):
                logger.info("Coder Node: Executing the plan.")
                model_name = choose_model_for_node("coder", state)
                system_prompt = f"""You are a Junior Software Developer working on a codebase at: {state["repo_path"]}
You are given a plan by the Architect. Execute it EXACTLY: make real code changes using the `write_file` tool.
- You MUST use `write_file` to apply changes. Do not just describe changes; write the actual file contents.
- Read each file with read_file first if needed, then call write_file with the full path and complete file content. Do not truncate.
- You must make at least one file change that addresses the user's request. When done, say 'I have completed the code changes.'"""
                if strict_rules:
                    system_prompt += "\n\nStrict Dev Fix Rules:\n" + strict_rules
                
                user_prompt = f"Architect's Plan:\n{state['plan']}\n\nPrevious Reviewer Feedback:\n{state.get('review_feedback', 'None. This is your first attempt.')}"
                coder_response = await call_claude_with_tools(system_prompt, user_prompt, model_name, "coder")
                logger.info("Coder response (first 500 chars): %s", (coder_response[:500] or "").strip())
                return {}

            # --- NODE: REVIEWER ---
            async def reviewer_node(state: AgentState):
                logger.info("Reviewer Node: Inspecting the codebase.")
                cycle = state.get("review_cycle", 0) + 1
                model_name = choose_model_for_node("reviewer", state)
                system_prompt = f"""You are a Tech Lead Code Reviewer for the codebase at: {state["repo_path"]}
A junior dev just finished changing code based on this User Request: {state["instruction"]}

You must use `get_git_diff` tool to see exactly what they changed.
Review their code for logical correctness, bugs, and if they fulfilled the user's request.
(Ignore minor accessibility Svelte warnings).

If the code looks perfect and solves the user request, your FINAL OUTPUT MUST explicitly contain the exact word "APPROVED".
If the code is wrong, incomplete, or if NO CHANGES WERE MADE, your FINAL OUTPUT MUST explicitly contain the exact word "REJECTED" followed by clear feedback for the junior dev to fix it."""
                if strict_rules:
                    system_prompt += "\n\nStrict Dev Fix Rules:\n" + strict_rules
                
                user_prompt = "Review the current state of the repository. Are the changes APPROVED or REJECTED?"
                feedback = await call_claude_with_tools(system_prompt, user_prompt, model_name, "reviewer")
                logger.info("Reviewer decision (cycle %s): %s", cycle, (feedback[:500] or "").strip())
                
                return {"review_feedback": feedback, "review_cycle": cycle}

            # --- ROUTING LOGIC ---
            def reviewer_route(state: AgentState):
                feedback = state.get("review_feedback", "")
                cycle = state.get("review_cycle", 0)
                if cycle >= MAX_REVIEW_CYCLES:
                    logger.info("Max review cycles reached; proceeding without further retries.")
                    return END
                if "APPROVED" in feedback.upper():
                    logger.info("Reviewer APPROVED the changes. Wrapping up.")
                    return END
                logger.info("Reviewer REJECTED the changes. Sending back to Coder.")
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
                    "review_feedback": "",
                    "review_cycle": 0,
                })
                return f"Development completed successfully!\n\nFinal Review Feedback:\n{final_state.get('review_feedback', 'Approved.')}"
            except Exception as e:
                logger.error(f"DevAgent execution failed: {e}")
                raise
