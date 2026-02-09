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

import asyncio
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.agent import get_agent_response
from app import voice_elevenlabs as voice


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
    return {"message": "AI Agent API", "docs": "/docs", "chat": "POST /chat", "voice": "/voice"}


@app.get("/voice")
async def voice_page():
    """Serve the Live Connect voice assistant page."""
    voice_path = STATIC_DIR / "voice.html"
    if voice_path.exists():
        return FileResponse(voice_path)
    raise HTTPException(status_code=404, detail="Voice page not found")


@app.get("/settings")
async def settings_page():
    """Serve the settings page (voice options, etc.)."""
    settings_path = STATIC_DIR / "settings.html"
    if settings_path.exists():
        return FileResponse(settings_path)
    raise HTTPException(status_code=404, detail="Settings page not found")


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


# ----- Voice (Live Connect) -----

class TTSRequest(BaseModel):
    """Request body for /voice/tts."""
    text: str


# Timeout for SDK voice calls so we never hang (seconds)
VOICE_SDK_TIMEOUT = 30.0


@app.get("/voice/check")
async def voice_check():
    """
    Check if voice (ElevenLabs) is configured. Returns ready=True if ELEVENLABS_API_KEY is set.
    Use this to confirm .env is loaded before testing /voice/welcome.
    """
    key = os.getenv("ELEVENLABS_API_KEY")
    return {"ready": bool(key and key.strip()), "message": "Set ELEVENLABS_API_KEY in .env" if not key else "OK"}


@app.get("/voice/welcome")
async def voice_welcome():
    """
    Returns audio (MP3) of the welcome phrase for Live Connect.
    Time-based: e.g. 'Good morning, how can I help you?'
    """
    phrase = voice.get_welcome_phrase()
    loop = asyncio.get_event_loop()
    try:
        audio_bytes = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: voice.text_to_speech(phrase)),
            timeout=VOICE_SDK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Voice welcome TTS timed out after %ss", VOICE_SDK_TIMEOUT)
        raise HTTPException(status_code=504, detail="TTS timed out. Try again.")
    if not audio_bytes:
        raise HTTPException(status_code=503, detail="ElevenLabs TTS not configured or failed. Set ELEVENLABS_API_KEY.")
    logger.info("Voice welcome: returning %s bytes", len(audio_bytes))
    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.post("/voice/tts")
async def voice_tts(body: TTSRequest):
    """Convert text to speech. Returns MP3 bytes."""
    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    loop = asyncio.get_event_loop()
    try:
        audio_bytes = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: voice.text_to_speech(body.text)),
            timeout=VOICE_SDK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Voice TTS timed out after %ss", VOICE_SDK_TIMEOUT)
        raise HTTPException(status_code=504, detail="TTS timed out.")
    if not audio_bytes:
        raise HTTPException(status_code=503, detail="ElevenLabs TTS not configured or failed.")
    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.post("/voice/stt")
async def voice_stt(file: UploadFile = File(...)):
    """Convert uploaded audio to text. Accepts audio/webm, audio/mpeg, etc."""
    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="No audio data")
    content_type = file.content_type or "audio/webm"
    loop = asyncio.get_event_loop()
    try:
        text = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: voice.speech_to_text(audio_bytes, content_type=content_type)
            ),
            timeout=VOICE_SDK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Voice STT timed out after %ss", VOICE_SDK_TIMEOUT)
        raise HTTPException(status_code=504, detail="STT timed out.")
    if text is None:
        raise HTTPException(status_code=503, detail="ElevenLabs STT failed or not configured.")
    return {"text": text}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
