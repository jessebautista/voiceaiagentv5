# main.py — Entry point: FastAPI server, web UI (chat, voice, workspace, settings),
# chat and voice routes to the agent, workspace config API (per-agent overrides,
# tools, welcome). Handles CORS and static files.

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
from typing import Optional

from app.agent import get_agent_response, get_agent_response_with_trace
from app import voice_elevenlabs as voice
from app.temporal_client import init_temporal_client, get_temporal_client

app = FastAPI(
    title="AI Agent Service",
    description="Receptionist-style AI agent with FAQ search, booking, and calculator tools.",
    version="1.0.0",
)

@app.on_event("startup")
async def startup_event():
    await init_temporal_client()

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


class ChatRequest(BaseModel):
    """Request body for /chat."""
    session_id: str
    message: str
    include_observability: bool = False  # when True, response includes trace (input, llm, tool calls, etc.)

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


@app.get("/workspace")
async def workspace_page():
    """Serve the workspace page: edit voice agent prompt, tools, welcome message."""
    workspace_path = STATIC_DIR / "workspace.html"
    if workspace_path.exists():
        return FileResponse(workspace_path)
    raise HTTPException(status_code=404, detail="Workspace page not found")


@app.get("/health")
async def health():
    """Health check for load balancers and monitoring."""
    return {"status": "ok"}


# ----- Workspace (edit agent behavior) -----

@app.get("/api/workspace/config")
async def workspace_get_config():
    """
    Return workspace config: prompt_key, prompt_keys, system_prompt (override for current
    agent from data/prompt_overrides/{key}.txt or null), default_system_prompt (base file),
    enabled_tools, welcome_message, tool_names.
    """
    from app.workspace_config import load_config, get_prompt_override, ALL_TOOL_NAMES, ALL_PROMPT_KEYS
    from app.agent import _read_prompt_for_key
    ws = load_config()
    pk = ws.get("prompt_key") or "receptionist"
    default_prompt = _read_prompt_for_key(pk)
    override = get_prompt_override(pk)
    return {
        "prompt_key": pk,
        "prompt_keys": ALL_PROMPT_KEYS,
        "system_prompt": override,
        "default_system_prompt": default_prompt,
        "enabled_tools": ws.get("enabled_tools"),
        "welcome_message": ws.get("welcome_message"),
        "tool_names": ALL_TOOL_NAMES,
    }


class WorkspaceConfigUpdate(BaseModel):
    """Optional fields for PATCH /api/workspace/config. system_prompt_override writes to data/prompt_overrides/{prompt_key}.txt."""
    prompt_key: Optional[str] = None
    system_prompt: Optional[str] = None  # backward compat: treated as system_prompt_override
    system_prompt_override: Optional[str] = None
    enabled_tools: Optional[list] = None
    welcome_message: Optional[str] = None


@app.patch("/api/workspace/config")
async def workspace_patch_config(body: WorkspaceConfigUpdate):
    """Update workspace config. Override is stored per-agent in data/prompt_overrides/{prompt_key}.txt."""
    from app.workspace_config import patch_config
    override = body.system_prompt_override if body.system_prompt_override is not None else body.system_prompt
    updated = patch_config(
        prompt_key=body.prompt_key,
        system_prompt_override=override,
        enabled_tools=body.enabled_tools,
        welcome_message=body.welcome_message,
    )
    return updated


@app.post("/chat")
async def chat(body: ChatRequest):
    """
    Send a message to the agent and get a response.
    session_id: Identifies the conversation thread (memory is keyed by this).
    message: The user's message.
    include_observability: if True, response includes trace (input, llm steps, tool calls, tool results).
    """
    if not body.message or not body.message.strip():
        return {"reply": "Please send a non-empty message.", "error": None, "trace": None}
    msg = body.message.strip()
    logger.info("Chat request | session_id=%s | message=%s", body.session_id, msg[:80] + ("..." if len(msg) > 80 else ""))
    try:
        if body.include_observability:
            reply, trace = await get_agent_response_with_trace(session_id=body.session_id, user_message=msg)
            logger.info("Chat response | session_id=%s | reply_len=%s | trace_steps=%s", body.session_id, len(reply), len(trace))
            return {"reply": reply, "error": None, "trace": trace}
        reply = await get_agent_response(session_id=body.session_id, user_message=msg)
        logger.info("Chat response | session_id=%s | reply_len=%s", body.session_id, len(reply))
        return {"reply": reply, "error": None, "trace": None}
    except Exception as e:
        logger.exception("Chat error | session_id=%s", body.session_id)
        return {"reply": "", "error": str(e), "trace": None}


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


# ----- Temporal API Routes -----

class EmailRequest(BaseModel):
    email: str

@app.post("/api/invitations")
async def start_invitation(body: EmailRequest):
    """Start an invitation workflow for an email address."""
    client = get_temporal_client()
    if not client:
        raise HTTPException(status_code=503, detail="Temporal client is not connected.")
    
    from app.workflows.invitation import InvitationWorkflow
    try:
        handle = await client.start_workflow(
            InvitationWorkflow.run,
            body.email,
            id=f"invitation-workflow-{body.email}",
            task_queue="voiceai-email-queue",
        )
        return {"status": "started", "workflow_id": handle.id}
    except Exception as e:
        logger.error(f"Failed to start workflow: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/invitations/accept")
async def accept_invitation(body: EmailRequest):
    """Signal the running workflow that the user has accepted."""
    client = get_temporal_client()
    if not client:
        raise HTTPException(status_code=503, detail="Temporal client is not connected.")
    
    from app.workflows.invitation import InvitationWorkflow
    workflow_id = f"invitation-workflow-{body.email}"
    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(InvitationWorkflow.user_responded)
        return {"status": "signaled", "message": f"Signaled workflow {workflow_id} as accepted."}
    except Exception as e:
        logger.error(f"Failed to signal workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to signal workflow. Is it running?")

@app.post("/api/agreements")
async def start_agreement(body: EmailRequest):
    """Start the 'Agreed' reminder workflow for an email address."""
    client = get_temporal_client()
    if not client:
        raise HTTPException(status_code=503, detail="Temporal client is not connected.")
    
    from app.workflows.agreed import AgreedWorkflow
    try:
        handle = await client.start_workflow(
            AgreedWorkflow.run,
            body.email,
            id=f"agreed-workflow-{body.email}",
            task_queue="voiceai-email-queue",
        )
        return {"status": "started", "workflow_id": handle.id}
    except Exception as e:
        logger.error(f"Failed to start workflow: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/agreements/adjudicate")
async def complete_adjudication(body: EmailRequest):
    """Signal the running 'Agreed' workflow that adjudication is complete."""
    client = get_temporal_client()
    if not client:
        raise HTTPException(status_code=503, detail="Temporal client is not connected.")
    
    from app.workflows.agreed import AgreedWorkflow
    workflow_id = f"agreed-workflow-{body.email}"
    try:
        handle = client.get_workflow_handle(workflow_id)
        await handle.signal(AgreedWorkflow.adjudication_completed)
        return {"status": "signaled", "message": f"Signaled workflow {workflow_id} adjudication complete."}
    except Exception as e:
        logger.error(f"Failed to signal workflow {workflow_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to signal workflow. Is it running?")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
