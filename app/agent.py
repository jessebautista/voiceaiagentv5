# agent.py — Orchestrator: Defines the "Brain" by wiring the LLM to tools (search_faq,
# booking, calculator) and optional Redis memory. Uses LangGraph's create_react_agent
# for reasoning and tool-calling. Loads system prompt from prompts/.

import os
from pathlib import Path

from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, AIMessage

from app.tools.search_faq import search_faq
from app.tools.booking import create_booking, list_available_slots
from app.tools.calculator import calculate

# Load system prompt
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_receptionist.txt"
SYSTEM_PROMPT = ""
if SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT = SYSTEM_PROMPT_PATH.read_text().strip()
else:
    SYSTEM_PROMPT = (
        "You are a helpful receptionist. Answer questions using the FAQ when relevant, "
        "help with bookings when asked, and use the calculator for pricing or math. "
        "Be concise and professional."
    )

# All tools the agent can use
TOOLS = [search_faq, create_booking, list_available_slots, calculate]

# LLM (use OpenAI; set OPENAI_API_KEY in .env). Use a placeholder if unset so imports succeed in tests.
llm = ChatOpenAI(
    model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
    temperature=0,
    api_key=os.getenv("OPENAI_API_KEY") or "sk-dummy",
)

# In-memory checkpointer for conversation history (thread_id = session_id)
# For production, swap to Redis/Postgres checkpointer.
checkpointer = MemorySaver()
agent = create_react_agent(
    llm,
    TOOLS,
    checkpointer=checkpointer,
    prompt=SYSTEM_PROMPT,
)


async def get_agent_response(
    session_id: str, user_message: str, callbacks: list = None
) -> str:
    """
    Run the agent for one user turn and return the final assistant reply.
    Uses session_id as thread_id so conversation history is preserved.
    callbacks: optional list of LangChain callback handlers for observability.
    """
    from app.callbacks import LoggingCallbackHandler
    handlers = callbacks if callbacks is not None else [LoggingCallbackHandler()]
    config = {"configurable": {"thread_id": session_id}, "callbacks": handlers}
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content=user_message)]},
        config=config,
    )
    messages = result.get("messages", [])
    # Last message should be from the assistant
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.content:
            return msg.content
    return "I didn't generate a reply. Please try again."
