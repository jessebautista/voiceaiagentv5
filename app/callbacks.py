# callbacks.py — LangChain callback handler that logs tool calls and LLM activity
# to the console for observability. on_tool_end accepts either a string or a
# ToolMessage (LangGraph) and normalizes to string for logging.

import logging
from typing import Any, Dict, List, Optional

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.outputs import LLMResult
from langchain_core.messages import BaseMessage

logger = logging.getLogger("app.agent")


def _truncate(s: str, max_len: int = 200) -> str:
    if not s or len(s) <= max_len:
        return s or ""
    return s[: max_len - 3].rstrip() + "..."


class LoggingCallbackHandler(BaseCallbackHandler):
    """Logs LLM and tool activity to the app logger."""

    def on_llm_start(self, serialized: Dict[str, Any], prompts: List[str], **kwargs: Any) -> None:
        logger.info("LLM call started (prompt length %s)", sum(len(p) for p in prompts))

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        try:
            if response.generations and response.generations[0]:
                gen = response.generations[0][0]
                text = getattr(gen, "text", None) or getattr(gen, "message", "")
                if hasattr(text, "content"):
                    text = text.content
                logger.info("LLM response: %s", _truncate(str(text)))
        except Exception:
            logger.debug("LLM end (could not extract text)")

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("LLM error: %s", error)

    def on_tool_start(self, serialized: Dict[str, Any], input_str: str, **kwargs: Any) -> None:
        name = serialized.get("name", "?")
        logger.info("Tool started: %s | input: %s", name, _truncate(input_str))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        # LangGraph may pass a ToolMessage; normalize to string for logging
        if hasattr(output, "content"):
            output = output.content if output.content is not None else ""
        else:
            output = str(output) if output is not None else ""
        logger.info("Tool ended | output: %s", _truncate(output))

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        logger.error("Tool error: %s", error)

    def on_agent_action(self, action: AgentAction, **kwargs: Any) -> None:
        logger.debug("Agent action: %s", action.tool)

    def on_agent_finish(self, finish: AgentFinish, **kwargs: Any) -> None:
        logger.debug("Agent finish: %s", _truncate(str(finish.return_values)))
