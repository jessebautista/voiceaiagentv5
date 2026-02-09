# logging_config.py — Configures application logging and optional LangSmith tracing.
# Call configure_logging() and optionally enable_langsmith() at startup.

import logging
import os


def configure_logging(level: str = None) -> None:
    """Set up logging format and level for the app and uvicorn."""
    level = level or os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format=fmt, datefmt=datefmt)
    # Reduce noise from third-party libs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def enable_langsmith() -> bool:
    """If LANGCHAIN_API_KEY is set, enable LangSmith tracing. Returns True if enabled."""
    api_key = os.getenv("LANGCHAIN_API_KEY")
    if not api_key:
        return False
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    if not os.getenv("LANGCHAIN_PROJECT"):
        os.environ["LANGCHAIN_PROJECT"] = "ai-agent-service"
    logging.getLogger(__name__).info("LangSmith tracing enabled (LANGCHAIN_API_KEY set)")
    return True
