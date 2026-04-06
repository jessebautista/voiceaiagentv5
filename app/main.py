# main.py — Entry point: FastAPI server, web UI (chat, voice, workspace, settings),
# chat and voice routes to the agent, workspace config API (per-agent overrides,
# tools, welcome). Handles CORS and static files.

import os
import re
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
from fastapi import FastAPI, File, UploadFile, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

from app.agent import get_agent_response, get_agent_response_with_trace
from app import voice_elevenlabs as voice
from app.temporal_client import init_temporal_client, get_temporal_client

app = FastAPI(
    title="AI Agent Service",
    description="Receptionist-style AI agent with FAQ search, booking, and calculator tools.",
    version="1.0.0",
)

# CORS: configurable for local vs deployed. Set CORS_ORIGINS in production to your PHWB origin(s).
_cors_origins_raw = os.getenv("CORS_ORIGINS", "").strip()
if _cors_origins_raw:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
else:
    # Default: localhost only (local dev). For deployed PHWB, set CORS_ORIGINS to your frontend URL(s).
    _cors_origins = [
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:3000",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    await init_temporal_client()

# Serve static files (web UI) from static/
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ChatRequest(BaseModel):
    """Request body for /chat."""
    session_id: str
    message: str
    include_observability: bool = False  # when True, response includes trace (input, llm, tool calls, etc.)

class BugFixRequest(BaseModel):
    """Request body for /api/dev/fix."""
    id: int
    title: str
    description: str
    category: str
    status: str
    module_hint: Optional[str] = None
    path_hint: Optional[str] = None
    labels: Optional[List[str]] = None
    test_mode: bool = False  # If True, skip LLM agent; apply trivial change and run verify → PR (for repo test).


class MigrationPreviewRequest(BaseModel):
    """Request body for /api/dev/fix/migrations/preview."""
    bug_id: int
    workflow_id: Optional[str] = None
    branch_name: Optional[str] = None


class MigrationApplyRequest(BaseModel):
    """Request body for /api/dev/fix/migrations/apply."""
    bug_id: int
    workflow_id: Optional[str] = None
    branch_name: Optional[str] = None
    confirm: bool = False
    confirm_destructive: bool = False
    dry_run: bool = False


def _enrich_bug_scope_hints(payload: dict) -> dict:
    """
    Best-effort enrichment for non-dev authored bug tickets.
    Pulls labels and common scope-like fields from Supabase by bug id if missing.
    Never raises; returns original payload when enrichment is unavailable.
    """
    try:
        from app.supabase_client import get_supabase_client
        supabase = get_supabase_client("DEV_AGENT")
        if not supabase:
            return payload

        bug_id = payload.get("id")
        if not isinstance(bug_id, int):
            return payload

        # Fetch bug row as-is, then dynamically inspect likely scope keys.
        bug_row = None
        try:
            bug_res = supabase.from_("phwb_bugs").select("*").eq("id", bug_id).maybe_single().execute()
            bug_row = getattr(bug_res, "data", None)
        except Exception:
            bug_row = None

        if isinstance(bug_row, dict):
            if not payload.get("module_hint"):
                for key in ("module", "area", "feature_area", "section", "team"):
                    val = bug_row.get(key)
                    if isinstance(val, str) and val.strip():
                        payload["module_hint"] = val.strip()
                        break
            if not payload.get("path_hint"):
                for key in ("path", "route", "page_path", "source_path", "file_path", "component_path"):
                    val = bug_row.get(key)
                    if isinstance(val, str) and val.strip():
                        payload["path_hint"] = val.strip()
                        break

        if not payload.get("labels"):
            label_names: List[str] = []
            try:
                la_res = (
                    supabase.from_("phwb_bug_label_assignments")
                    .select("label_id")
                    .eq("bug_id", bug_id)
                    .execute()
                )
                assignments = getattr(la_res, "data", None) or []
                label_ids = [row.get("label_id") for row in assignments if isinstance(row, dict) and row.get("label_id") is not None]
                if label_ids:
                    labels_res = supabase.from_("phwb_bug_labels").select("id,name").in_("id", label_ids).execute()
                    label_rows = getattr(labels_res, "data", None) or []
                    label_names = [
                        row.get("name", "").strip()
                        for row in label_rows
                        if isinstance(row, dict) and isinstance(row.get("name"), str) and row.get("name", "").strip()
                    ]
            except Exception:
                label_names = []

            if label_names:
                payload["labels"] = label_names
    except Exception:
        return payload
    return payload


def _extract_branch_from_logs(log_rows: List[dict]) -> Optional[str]:
    for row in log_rows:
        msg = str(row.get("message") or "")
        m = re.search(r"Branch `([^`]+)` created", msg)
        if m:
            return m.group(1).strip()
    return None


def _extract_migration_files_from_logs(log_rows: List[dict]) -> List[str]:
    files: List[str] = []
    for row in log_rows:
        msg = str(row.get("message") or "")
        # verify_fix metadata style: migration_files=a.sql,b.sql
        m = re.search(r"migration_files=([^\n]+)", msg)
        if m:
            raw = m.group(1).strip()
            if raw and raw != "(none)":
                files.extend([p.strip() for p in raw.split(",") if p.strip()])
        # workflow_state style: DB migration changes detected: migrations/a.sql, migrations/b.sql
        m2 = re.search(r"DB migration changes detected:\s*(.+)$", msg)
        if m2:
            files.extend([p.strip() for p in m2.group(1).split(",") if p.strip()])

    seen: set[str] = set()
    deduped: List[str] = []
    for f in files:
        norm = f.replace("\\", "/").lstrip("./")
        if norm and norm not in seen and norm.startswith("migrations/") and norm.endswith(".sql"):
            seen.add(norm)
            deduped.append(norm)
    return deduped[:100]


def _parse_repo_name(repo_url: str) -> Optional[str]:
    if not repo_url or "github.com/" not in repo_url:
        return None
    name = repo_url.split("github.com/")[-1].strip()
    if name.endswith(".git"):
        name = name[:-4]
    return name or None


def _summarize_migration_sql(sql: str) -> Dict[str, Any]:
    text = sql or ""
    lower = text.lower()
    operations: List[str] = []
    tables: List[str] = []
    risk_tags: List[str] = []

    for m in re.finditer(r"create\s+table(?:\s+if\s+not\s+exists)?\s+([a-zA-Z0-9_\.]+)", lower, re.IGNORECASE):
        operations.append("create_table")
        tables.append(m.group(1))
    for m in re.finditer(r"alter\s+table\s+([a-zA-Z0-9_\.]+)\s+add\s+column", lower, re.IGNORECASE):
        operations.append("alter_add_column")
        tables.append(m.group(1))
    for m in re.finditer(r"alter\s+table\s+([a-zA-Z0-9_\.]+)\s+drop\s+column", lower, re.IGNORECASE):
        operations.append("alter_drop_column")
        tables.append(m.group(1))
    for m in re.finditer(r"create\s+index(?:\s+if\s+not\s+exists)?\s+[a-zA-Z0-9_]+\s+on\s+([a-zA-Z0-9_\.]+)", lower, re.IGNORECASE):
        operations.append("create_index")
        tables.append(m.group(1))
    for m in re.finditer(r"drop\s+table(?:\s+if\s+exists)?\s+([a-zA-Z0-9_\.]+)", lower, re.IGNORECASE):
        operations.append("drop_table")
        tables.append(m.group(1))

    if re.search(r"\bdrop\s+(table|column|constraint)\b|\btruncate\b", lower):
        risk_tags.append("destructive")
    if re.search(r"\b(create|alter|drop)\s+table\b", lower):
        risk_tags.append("ddl")

    op_set = set(operations)
    if op_set and op_set.issubset({"create_index"}):
        risk_tags.append("index_only")

    # Dedupe while preserving order
    def _dedupe(items: List[str]) -> List[str]:
        seen: set[str] = set()
        out: List[str] = []
        for i in items:
            if i not in seen:
                seen.add(i)
                out.append(i)
        return out

    operations = _dedupe(operations)
    tables = _dedupe(tables)
    risk_tags = _dedupe(risk_tags)

    return {
        "operations": operations,
        "touched_tables": tables,
        "risk_tags": risk_tags,
    }


def _insert_dev_log(
    supabase: Any,
    *,
    bug_id: int,
    step: str,
    message: str,
    level: str = "info",
    workflow_id: Optional[str] = None,
) -> None:
    try:
        supabase.table("phwb_dev_logs").insert(
            {
                "bug_id": bug_id,
                "step": step,
                "message": message,
                "level": level,
                "workflow_id": workflow_id,
            }
        ).execute()
    except Exception as e:
        logger.warning("Failed to insert dev log row: %s", e)


def _insert_bug_comment(
    supabase: Any,
    *,
    bug_id: int,
    content: str,
    is_internal: bool = False,
) -> None:
    try:
        supabase.table("phwb_bug_comments").insert(
            {
                "bug_id": bug_id,
                "user_id": None,
                "content": content,
                "is_internal": is_internal,
            }
        ).execute()
    except Exception as e:
        logger.warning("Failed to insert bug comment: %s", e)


def _latest_workflow_state_from_logs(rows: List[Dict[str, Any]]) -> Optional[str]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("step") or "") != "workflow_state":
            continue
        msg = str(row.get("message") or "")
        m = re.search(r"`([^`]+)`", msg)
        if m:
            return m.group(1).strip()
    return None


def _rollback_guidance(failed_files: List[str], target: str) -> List[str]:
    file_hint = ", ".join(failed_files[:5]) if failed_files else "the failed migration files"
    return [
        f"Stop further DB apply attempts on target `{target}` until root cause is identified.",
        f"Review SQL and execution errors for {file_hint}.",
        "If partial changes were applied, prepare a corrective migration (forward-fix) instead of manual table edits.",
        "Restore from backup/snapshot only when forward-fix is not viable and impact is confirmed.",
        "Re-run `/api/dev/fix/migrations/preview` and execute a dry-run before re-attempting apply.",
    ]


def _apply_sql_via_rpc(supabase: Any, sql: str) -> None:
    rpc_name = (os.getenv("DEV_AGENT_DB_APPLY_RPC_NAME") or "exec_sql").strip()
    rpc_arg = (os.getenv("DEV_AGENT_DB_APPLY_RPC_ARG") or "sql").strip()
    supabase.rpc(rpc_name, {rpc_arg: sql}).execute()

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

@app.post("/api/dev/fix")
async def start_dev_fix(body: BugFixRequest):
    """Start the Development Agent workflow for a bug fix."""
    client = get_temporal_client()
    if not client:
        raise HTTPException(status_code=503, detail="Temporal client is not connected.")
    
    import time
    from app.workflows.dev_fix import DevFixWorkflow
    try:
        # Add timestamp to workflow ID so each click creates a new unique run
        workflow_id = f"dev-fix-workflow-{body.id}-{int(time.time())}"
        enriched_bug_payload = _enrich_bug_scope_hints(body.dict())
        handle = await client.start_workflow(
            DevFixWorkflow.run,
            enriched_bug_payload,
            id=workflow_id,
            task_queue="voiceai-email-queue-v3",
        )
        logger.info(f"Started dev fix workflow: {workflow_id}")
        return {"status": "started", "workflow_id": handle.id}
    except Exception as e:
        logger.error(f"Failed to start dev fix workflow: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dev/fix/migrations/preview")
async def dev_fix_migrations_preview(body: MigrationPreviewRequest):
    """
    Preview migration changes for a bug/workflow:
    - infers migration file list from dev logs
    - resolves branch (from request or logs)
    - fetches SQL from GitHub branch and returns summary + risk tags
    """
    from app.supabase_client import get_supabase_client

    supabase = get_supabase_client("DEV_AGENT")
    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase (DEV_AGENT) is not configured.")

    try:
        q = (
            supabase.from_("phwb_dev_logs")
            .select("step,message,workflow_id,created_at")
            .eq("bug_id", body.bug_id)
            .order("created_at", desc=True)
            .limit(300)
        )
        if body.workflow_id:
            q = q.eq("workflow_id", body.workflow_id)
        res = q.execute()
        rows = getattr(res, "data", None) or []
    except Exception as e:
        logger.error("Failed to query dev logs for migration preview: %s", e)
        raise HTTPException(status_code=500, detail="Failed to read dev logs.")

    branch_name = (body.branch_name or "").strip() or _extract_branch_from_logs(rows) or ""
    migration_files = _extract_migration_files_from_logs(rows)
    db_changes_detected = len(migration_files) > 0

    response: Dict[str, Any] = {
        "bug_id": body.bug_id,
        "workflow_id": body.workflow_id,
        "db_changes_detected": db_changes_detected,
        "requires_db_confirmation": db_changes_detected,
        "branch_name": branch_name or None,
        "migration_files": migration_files,
        "summaries": [],
        "warnings": [],
    }

    if not db_changes_detected:
        return response
    if not branch_name:
        response["warnings"].append("Branch name not found. Provide branch_name to fetch SQL summaries.")
        return response

    github_token = os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN", "").strip()
    repo_url = os.getenv("DEV_AGENT_GITHUB_REPO_URL", "").strip()
    repo_name = _parse_repo_name(repo_url)
    if not github_token or not repo_name:
        response["warnings"].append("GitHub token/repo not configured; returning filenames only.")
        return response

    try:
        from github import Github

        gh = Github(github_token)
        repo = gh.get_repo(repo_name)
    except Exception as e:
        logger.warning("Could not initialize GitHub client for migration preview: %s", e)
        response["warnings"].append("Unable to initialize GitHub client; returning filenames only.")
        return response

    for file_path in migration_files:
        entry: Dict[str, Any] = {"file": file_path, "summary": {}, "error": None}
        try:
            content_obj = repo.get_contents(file_path, ref=branch_name)
            decoded = content_obj.decoded_content.decode("utf-8", errors="replace")
            entry["summary"] = _summarize_migration_sql(decoded)
        except Exception as e:
            entry["error"] = f"Failed to fetch/parse SQL: {e}"
        response["summaries"].append(entry)

    return response


@app.post("/api/dev/fix/migrations/apply")
async def dev_fix_migrations_apply(
    body: MigrationApplyRequest,
    x_dev_agent_db_token: Optional[str] = Header(default=None),
):
    """
    Apply migration SQL files for a dev-fix run.
    Protected by environment guards + optional confirmation/token.
    """
    from app.supabase_client import get_supabase_client

    apply_enabled = (os.getenv("DEV_AGENT_DB_AUTO_APPLY_ENABLED") or "").strip().lower() in ("1", "true", "yes")
    requires_confirmation = (os.getenv("DEV_AGENT_DB_APPLY_REQUIRES_CONFIRMATION") or "1").strip().lower() in ("1", "true", "yes")
    apply_target = (os.getenv("DEV_AGENT_DB_APPLY_TARGET") or "dev").strip() or "dev"
    required_token = (os.getenv("DEV_AGENT_DB_APPLY_TOKEN") or "").strip()

    if not apply_enabled:
        raise HTTPException(status_code=403, detail="DB migration apply is disabled by configuration.")
    if requires_confirmation and not body.confirm:
        raise HTTPException(status_code=400, detail="Confirmation is required before applying DB changes.")
    if required_token and (x_dev_agent_db_token or "") != required_token:
        raise HTTPException(status_code=403, detail="Invalid DB apply token.")

    supabase = get_supabase_client("DEV_AGENT")
    if not supabase:
        raise HTTPException(status_code=503, detail="Supabase (DEV_AGENT) is not configured.")

    # Load dev logs for workflow context.
    try:
        q = (
            supabase.from_("phwb_dev_logs")
            .select("step,message,workflow_id,created_at")
            .eq("bug_id", body.bug_id)
            .order("created_at", desc=True)
            .limit(300)
        )
        if body.workflow_id:
            q = q.eq("workflow_id", body.workflow_id)
        res = q.execute()
        rows = getattr(res, "data", None) or []
    except Exception as e:
        logger.error("Failed to query dev logs for migration apply: %s", e)
        raise HTTPException(status_code=500, detail="Failed to read dev logs.")

    branch_name = (body.branch_name or "").strip() or _extract_branch_from_logs(rows) or ""
    migration_files = _extract_migration_files_from_logs(rows)
    migration_files = sorted(migration_files)
    db_changes_detected = len(migration_files) > 0

    _insert_dev_log(
        supabase,
        bug_id=body.bug_id,
        step="db_apply",
        message=(
            f"🧪 DB apply requested (target={apply_target}, dry_run={str(body.dry_run).lower()}). "
            f"migrations={','.join(migration_files[:10]) if migration_files else '(none)'}"
        ),
        level="info",
        workflow_id=body.workflow_id,
    )

    result: Dict[str, Any] = {
        "bug_id": body.bug_id,
        "workflow_id": body.workflow_id,
        "target": apply_target,
        "dry_run": body.dry_run,
        "branch_name": branch_name or None,
        "db_changes_detected": db_changes_detected,
        "migration_files": migration_files,
        "applied": [],
        "failed": [],
        "success": True,
        "warnings": [],
        "rollback_guidance": [],
    }

    if not db_changes_detected:
        _insert_dev_log(
            supabase,
            bug_id=body.bug_id,
            step="db_apply",
            message="ℹ️ No migration files detected; nothing to apply.",
            level="info",
            workflow_id=body.workflow_id,
        )
        return result

    if not branch_name:
        raise HTTPException(status_code=400, detail="Branch name not found. Provide branch_name or run setup logs first.")

    github_token = (os.getenv("DEV_AGENT_GITHUB_ACCESS_TOKEN") or "").strip()
    repo_url = (os.getenv("DEV_AGENT_GITHUB_REPO_URL") or "").strip()
    repo_name = _parse_repo_name(repo_url)
    if not github_token or not repo_name:
        raise HTTPException(status_code=503, detail="GitHub token/repo not configured for migration apply.")

    try:
        from github import Github

        gh = Github(github_token)
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize GitHub client: {e}")

    prepared: List[Dict[str, Any]] = []
    for file_path in migration_files:
        entry: Dict[str, Any] = {"file": file_path}
        try:
            content_obj = repo.get_contents(file_path, ref=branch_name)
            sql = content_obj.decoded_content.decode("utf-8", errors="replace")
            summary = _summarize_migration_sql(sql)
            entry["summary"] = summary
            prepared.append({**entry, "sql": sql})
        except Exception as e:
            result["success"] = False
            result["failed"].append({**entry, "error": str(e)})

    destructive_files = [
        p.get("file")
        for p in prepared
        if isinstance(p, dict)
        and isinstance(p.get("summary"), dict)
        and "destructive" in ((p.get("summary") or {}).get("risk_tags") or [])
    ]
    if destructive_files and not body.confirm_destructive:
        msg = (
            "Destructive migration apply requires explicit confirmation "
            "(confirm_destructive=true). Files: " + ", ".join([str(f) for f in destructive_files[:10]])
        )
        _insert_dev_log(
            supabase,
            bug_id=body.bug_id,
            step="db_apply",
            message="⚠️ " + msg,
            level="warning",
            workflow_id=body.workflow_id,
        )
        raise HTTPException(status_code=400, detail=msg)

    for prepared_item in prepared:
        entry = {"file": prepared_item.get("file"), "summary": prepared_item.get("summary")}
        try:
            if body.dry_run:
                result["applied"].append({**entry, "dry_run": True})
                continue
            sql = str(prepared_item.get("sql") or "")
            _apply_sql_via_rpc(supabase, sql)
            result["applied"].append(entry)
        except Exception as e:
            result["success"] = False
            result["failed"].append({**entry, "error": str(e)})

    # Post-apply targeted verification: probe touched tables.
    post_apply_checks: List[Dict[str, Any]] = []
    if not body.dry_run and result["success"]:
        touched_tables: List[str] = []
        for item in result["applied"]:
            summary = item.get("summary") if isinstance(item, dict) else None
            if isinstance(summary, dict):
                for t in summary.get("touched_tables", []) or []:
                    if isinstance(t, str) and t.strip():
                        table = t.strip().replace('"', "")
                        if table.lower().startswith("public."):
                            table = table.split(".", 1)[1]
                        touched_tables.append(table)

        seen_tables: set[str] = set()
        for table in touched_tables:
            if table in seen_tables:
                continue
            seen_tables.add(table)
            check_entry: Dict[str, Any] = {"table": table, "ok": True, "error": None}
            try:
                # Probe table existence/readability after migration apply.
                supabase.from_(table).select("*").limit(1).execute()
            except Exception as e:
                check_entry["ok"] = False
                check_entry["error"] = str(e)
                result["success"] = False
            post_apply_checks.append(check_entry)

    result["post_apply_checks"] = post_apply_checks

    if result["success"]:
        _insert_dev_log(
            supabase,
            bug_id=body.bug_id,
            step="db_apply",
            message=f"✅ DB apply completed. applied={len(result['applied'])}, failed=0",
            level="success",
            workflow_id=body.workflow_id,
        )
        if not body.dry_run:
            latest_state = _latest_workflow_state_from_logs(rows)
            has_pr_signal = any(
                isinstance(r, dict)
                and str(r.get("step") or "") in ("create_pull_request", "update_bug_ticket")
                and (
                    "PR:" in str(r.get("message") or "")
                    or "Pull Request" in str(r.get("message") or "")
                )
                for r in rows
            )
            if latest_state == "staging_validated" and has_pr_signal:
                state_msg = "🔖 Workflow state → `fully_complete`. Staging validated, DB apply succeeded, and PR is available."
                state_level = "success"
            elif latest_state == "staging_validated":
                state_msg = "🔖 Workflow state → `staging_validated`. DB apply succeeded; waiting PR creation/availability before fully_complete."
                state_level = "info"
            elif has_pr_signal:
                state_msg = "🔖 Workflow state → `ready_for_pr`. DB apply succeeded; waiting staging validation before fully_complete."
                state_level = "info"
            else:
                state_msg = "🔖 Workflow state → `staging_ready`. DB apply succeeded and post-apply checks passed."
                state_level = "info"

            _insert_dev_log(
                supabase,
                bug_id=body.bug_id,
                step="workflow_state",
                message=state_msg,
                level=state_level,
                workflow_id=body.workflow_id,
            )
            _insert_bug_comment(
                supabase,
                bug_id=body.bug_id,
                content=state_msg,
                is_internal=False,
            )
    else:
        failed_files = [str((f or {}).get("file") or "") for f in result["failed"] if isinstance(f, dict)]
        result["rollback_guidance"] = _rollback_guidance(failed_files, apply_target)
        _insert_dev_log(
            supabase,
            bug_id=body.bug_id,
            step="db_apply",
            message=(
                f"❌ DB apply completed with failures. applied={len(result['applied'])}, "
                f"failed={len(result['failed'])}"
            ),
            level="error",
            workflow_id=body.workflow_id,
        )
        _insert_dev_log(
            supabase,
            bug_id=body.bug_id,
            step="db_apply",
            message="Rollback guidance: " + " | ".join(result["rollback_guidance"][:3]),
            level="warning",
            workflow_id=body.workflow_id,
        )
        _insert_bug_comment(
            supabase,
            bug_id=body.bug_id,
            content=(
                "⚠️ DB migration apply failed.\n\n"
                + "\n".join(f"- {line}" for line in result["rollback_guidance"])
            ),
            is_internal=False,
        )
        if not body.dry_run:
            _insert_dev_log(
                supabase,
                bug_id=body.bug_id,
                step="workflow_state",
                message=(
                    "🔖 Workflow state → `code_complete_db_pending`. "
                    "DB apply failed or post-apply checks did not pass."
                ),
                level="warning",
                workflow_id=body.workflow_id,
            )

    return result


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
