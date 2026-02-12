# agent.py — Orchestrator: Defines the "Brain" by wiring the LLM to tools (search_faq,
# booking, calculator, etc.) and optional Redis memory. Uses LangGraph's create_react_agent.
# System prompt and enabled tools can be overridden via workspace config (data/workspace_config.json).

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

# Name -> tool for workspace enable/disable
TOOLS_BY_NAME = {
    "search_faq": search_faq,
    "create_booking": create_booking,
    "list_available_slots": list_available_slots,
    "calculate": calculate,
    "get_current_datetime": get_current_datetime,
    "get_reference_info": get_reference_info,
}

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_receptionist.txt"
DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful receptionist. Answer questions using the FAQ when relevant, "
    "help with bookings when asked, and use the calculator for pricing or math. "
    "Be concise and professional."
)


def _read_default_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text().strip()
    return DEFAULT_SYSTEM_PROMPT


def _get_system_prompt(workspace: dict) -> str:
    if workspace.get("system_prompt"):
        return workspace["system_prompt"].strip()
    return _read_default_system_prompt()


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


def get_agent():
    """Return the react agent, built from current workspace config (prompt + enabled tools)."""
    global _agent, _agent_config_hash
    from app.workspace_config import load_config, config_hash
    ws = load_config()
    h = config_hash(ws)
    if _agent_config_hash != h:
        prompt = _get_system_prompt(ws)
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
    """
    from app.callbacks import LoggingCallbackHandler
    handlers = callbacks if callbacks is not None else [LoggingCallbackHandler()]
    config = {"configurable": {"thread_id": session_id}, "callbacks": handlers}
    agent = get_agent()
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
        config=config,
    )
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return "I didn't generate a reply. Please try again."


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
    """Run the agent for one user turn; return (final_reply, trace)."""
    from app.callbacks import LoggingCallbackHandler
    handlers = callbacks if callbacks is not None else [LoggingCallbackHandler()]
    config = {"configurable": {"thread_id": session_id}, "callbacks": handlers}
    agent = get_agent()
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
        config=config,
    )
    messages = result.get("messages", [])
    reply = "I didn't generate a reply. Please try again."
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            reply = msg.content
            break
    return reply, _messages_to_trace(messages, user_message)
