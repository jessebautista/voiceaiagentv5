# main.py — Entry point: Starts the FastAPI server, serves the web UI, and routes
# chat requests to the agent. Handles CORS and static files for the simple web interface.

import os
from pathlib import Path

# Load .env from project root so OPENAI_API_KEY (and other vars) are set before the agent is imported
_root = Path(__file__).resolve().parent.parent
_env_file = _root / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_file)
    except ImportError:
        pass

from app.logging_config import configure_logging, enable_langsmith
configure_logging()
enable_langsmith()

import logging
logger = logging.getLogger("app.main")

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.agent import get_agent_response


class ChatRequest(BaseModel):
    """Request body for /chat."""
    session_id: str
    message: str

app = FastAPI(
    title="AI Agent Service",
    description="Receptionist-style AI agent with FAQ search, booking, and calculator tools.",
    version="1.0.0",
)

# Allow the web interface to call the API from any origin (adjust in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files (web UI) from static/
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    """Serve the chat web interface at the root."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "AI Agent API", "docs": "/docs", "chat": "POST /chat"}


@app.get("/health")
async def health():
    """Health check for load balancers and monitoring."""
    return {"status": "ok"}


@app.post("/chat")
async def chat(body: ChatRequest):
    """
    Send a message to the agent and get a response.
    session_id: Identifies the conversation thread (memory is keyed by this).
    message: The user's message.
    """
    if not body.message or not body.message.strip():
        return {"reply": "Please send a non-empty message.", "error": None}
    msg = body.message.strip()
    logger.info("Chat request | session_id=%s | message=%s", body.session_id, msg[:80] + ("..." if len(msg) > 80 else ""))
    try:
        reply = await get_agent_response(session_id=body.session_id, user_message=msg)
        logger.info("Chat response | session_id=%s | reply_len=%s", body.session_id, len(reply))
        return {"reply": reply, "error": None}
    except Exception as e:
        logger.exception("Chat error | session_id=%s", body.session_id)
        return {"reply": "", "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
