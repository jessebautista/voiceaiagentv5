# agent.py — Orchestrator: Defines the "Brain" by wiring the LLM to tools (search_faq,
# booking, Supabase CRUD, etc.). Uses LangGraph's create_react_agent. Prompt and tools
# come from workspace config (prompt_key, per-agent override in data/prompt_overrides, enabled_tools).
# Integrates episodic and procedural memory recall (Redis) for stateful AI behavior.

import os
from pathlib import Path

from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage, ToolMessage

from app.tools.search_faq import search_faq
from app.tools.booking import create_booking, list_available_slots
from app.tools.calculator import calculate
from app.tools.current_datetime import get_current_datetime
from app.tools.reference_json import get_reference_info
from app.tools.query_builder import build_supabase_query, list_supabase_tables
from app.tools.query_processor import execute_supabase_query

# Name -> tool for workspace enable/disable
TOOLS_BY_NAME = {
    "search_faq": search_faq,
    "create_booking": create_booking,
    "list_available_slots": list_available_slots,
    "calculate": calculate,
    "get_current_datetime": get_current_datetime,
    "get_reference_info": get_reference_info,
    "list_supabase_tables": list_supabase_tables,
    "build_supabase_query": build_supabase_query,
    "execute_supabase_query": execute_supabase_query,
}

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
# Map workspace prompt_key to prompt filename (without .txt)
PROMPT_KEY_TO_FILE = {"receptionist": "system_receptionist", "supabase_receptionist": "supabase_receptionist"}
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful receptionist. Answer questions using the FAQ when relevant, "
    "help with bookings when asked, and use the calculator for pricing or math. "
    "Be concise and professional."
)


def _read_prompt_for_key(prompt_key: str) -> str:
    """Load base prompt from app/prompts/{filename}.txt for the given prompt_key."""
    filename = PROMPT_KEY_TO_FILE.get(prompt_key, "system_receptionist")
    path = PROMPTS_DIR / f"{filename}.txt"
    if path.exists():
        return path.read_text().strip()
    return DEFAULT_SYSTEM_PROMPT


def _get_system_prompt(workspace: dict, memory_context: str = "") -> str:
    """
    Get system prompt with optional memory context injected.
    memory_context: Formatted string from recall_memories_for_agent (empty if no memories).
    """
    from app.workspace_config import get_prompt_override
    prompt_key = workspace.get("prompt_key") or "receptionist"
    override = get_prompt_override(prompt_key)
    base_prompt = override if override else _read_prompt_for_key(prompt_key)
    
    # Inject memory context if provided
    if memory_context:
        return f"{base_prompt}{memory_context}"
    return base_prompt


def _get_enabled_tools(workspace: dict):
    names = workspace.get("enabled_tools")
    if names:
        return [TOOLS_BY_NAME[n] for n in names if n in TOOLS_BY_NAME]
    return list(TOOLS_BY_NAME.values())


# LLM (use OpenAI; set OPENAI_API_KEY in .env)
llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY") or "sk-dummy",
)
checkpointer = MemorySaver()

# Cached agent; rebuilt when workspace config changes
_agent = None
_agent_config_hash = None


def get_agent(memory_context: str = ""):
    """
    Return the react agent, built from current workspace config (prompt + enabled tools).
    memory_context: Optional memory context to inject into prompt (typically empty here;
    memory is injected per-request in get_agent_response).
    """
    global _agent, _agent_config_hash
    from app.workspace_config import load_config, config_hash
    ws = load_config()
    h = config_hash(ws)
    # Note: memory_context changes per request, so we don't cache agent with it
    # Instead, we inject memory in get_agent_response before invoking
    if _agent_config_hash != h:
        prompt = _get_system_prompt(ws, memory_context="")
        tools = _get_enabled_tools(ws)
        _agent = create_react_agent(llm, tools, checkpointer=checkpointer, prompt=prompt)
        _agent_config_hash = h
    return _agent


async def get_agent_response(
    session_id: str, user_message: str, callbacks: list = None
) -> str:
    """
    Run the agent for one user turn and return the final assistant reply.
    Uses session_id as thread_id so conversation history is preserved.
    Recalls episodic/procedural memories and extracts new facts after response.
    """
    from app.callbacks import LoggingCallbackHandler
    from app.memory.recall import recall_memories_for_agent
    from app.memory.episodic import (
        extract_facts_from_conversation,
        store_fact,
        store_event,
        extract_user_id,
    )
    from app.memory.procedural import (
        infer_procedure_name,
        record_procedure_execution,
    )
    
    handlers = callbacks if callbacks is not None else [LoggingCallbackHandler()]
    
    # Recall relevant memories (cost-controlled, ~500 tokens)
    memory_context = recall_memories_for_agent(session_id, user_message, max_tokens=500)
    
    # If memory context exists, inject it into the first message
    # Note: LangGraph's create_react_agent uses the system prompt, but we can also
    # inject memory as a system message or prepend to user message
    enhanced_user_message = user_message
    if memory_context:
        # Prepend memory context to user message so agent sees it
        enhanced_user_message = f"[Context from past interactions: {memory_context.strip()}]\n\n{user_message}"
    
    config = {
        "configurable": {"thread_id": session_id},
        "callbacks": handlers,
        "recursion_limit": 50,  # allow multi-step tool flows (e.g. Supabase list → build → execute)
    }
    agent = get_agent()
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=enhanced_user_message)]},
        config=config,
    )
    messages = result.get("messages", [])
    
    # Extract final reply
    reply = "I didn't generate a reply. Please try again."
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break
    
    # Extract and store memories after response
    try:
        user_id = extract_user_id(session_id)
        # Extract facts from conversation
        facts = extract_facts_from_conversation(session_id, user_message, reply, messages)
        for fact in facts:
            store_fact(user_id, fact, confidence=0.8)
        
        # Track procedure execution if applicable
        tool_calls = []
        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                tool_calls.extend(msg.tool_calls or [])
        
        proc_name = infer_procedure_name(user_message, tool_calls)
        if proc_name:
            # Determine success (simple: if we got a reply and no errors)
            success = reply and "error" not in reply.lower() and "trouble" not in reply.lower()
            record_procedure_execution(
                proc_name,
                success=success,
                steps=[tc.get("name", "") for tc in tool_calls if tc.get("name")],
                user_id=user_id,
            )
        
        # Store event if significant action occurred
        if tool_calls:
            action_names = [tc.get("name", "") for tc in tool_calls]
            if "execute_supabase_query" in action_names:
                store_event(user_id, f"Executed database operation: {proc_name or 'unknown'}")
    except Exception:
        # Memory extraction failures shouldn't break the response
        pass
    
    return reply


def _messages_to_trace(messages: list, user_message: str) -> list:
    """Build a user-facing trace from the message list for observability."""
    trace = []
    last_human_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break
    if last_human_idx < 0:
        return trace
    for msg in messages[last_human_idx:]:
        if isinstance(msg, HumanMessage):
            content = (msg.content or "").strip()
            if content:
                trace.append({"type": "input", "content": content})
        elif isinstance(msg, AIMessage):
            content = (msg.content or "").strip()
            tool_calls = getattr(msg, "tool_calls", None) or []
            if content:
                trace.append({"type": "llm", "content": content})
            for tc in tool_calls:
                trace.append({
                    "type": "tool_call",
                    "name": tc.get("name", "?"),
                    "input": tc.get("args", {}),
                })
        elif isinstance(msg, ToolMessage):
            trace.append({
                "type": "tool_result",
                "name": getattr(msg, "name", "tool"),
                "output": (msg.content or "").strip(),
            })
    return trace


async def get_agent_response_with_trace(
    session_id: str, user_message: str, callbacks: list = None
) -> tuple:
    """
    Run the agent for one user turn; return (final_reply, trace).
    Includes memory recall and extraction (same as get_agent_response).
    """
    from app.callbacks import LoggingCallbackHandler
    from app.memory.recall import recall_memories_for_agent
    from app.memory.episodic import (
        extract_facts_from_conversation,
        store_fact,
        store_event,
        extract_user_id,
    )
    from app.memory.procedural import (
        infer_procedure_name,
        record_procedure_execution,
    )
    
    handlers = callbacks if callbacks is not None else [LoggingCallbackHandler()]
    
    # Recall relevant memories
    memory_context = recall_memories_for_agent(session_id, user_message, max_tokens=500)
    enhanced_user_message = user_message
    if memory_context:
        enhanced_user_message = f"[Context from past interactions: {memory_context.strip()}]\n\n{user_message}"
    
    config = {
        "configurable": {"thread_id": session_id},
        "callbacks": handlers,
        "recursion_limit": 50,  # allow multi-step tool flows (e.g. Supabase list → build → execute)
    }
    agent = get_agent()
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=enhanced_user_message)]},
        config=config,
    )
    messages = result.get("messages", [])
    reply = "I didn't generate a reply. Please try again."
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break
    
    # Extract and store memories
    try:
        user_id = extract_user_id(session_id)
        facts = extract_facts_from_conversation(session_id, user_message, reply, messages)
        for fact in facts:
            store_fact(user_id, fact, confidence=0.8)
        
        tool_calls = []
        for msg in messages:
            if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls"):
                tool_calls.extend(msg.tool_calls or [])
        
        proc_name = infer_procedure_name(user_message, tool_calls)
        if proc_name:
            success = reply and "error" not in reply.lower() and "trouble" not in reply.lower()
            record_procedure_execution(
                proc_name,
                success=success,
                steps=[tc.get("name", "") for tc in tool_calls if tc.get("name")],
                user_id=user_id,
            )
        
        if tool_calls:
            action_names = [tc.get("name", "") for tc in tool_calls]
            if "execute_supabase_query" in action_names:
                store_event(user_id, f"Executed database operation: {proc_name or 'unknown'}")
    except Exception:
        pass
    
    return reply, _messages_to_trace(messages, user_message)
