# workspace_config.py — Read/write workspace settings (prompt key, per-agent prompt
# overrides, enabled tools, welcome message). prompt_key selects which base prompt
# file to use; each agent can have its own override file in data/prompt_overrides/{key}.txt.

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "workspace_config.json"
PROMPT_OVERRIDES_DIR = Path(__file__).resolve().parent.parent / "data" / "prompt_overrides"

# Prompt keys: which base prompt file to load (app/prompts/{key}.txt)
ALL_PROMPT_KEYS = ["receptionist", "supabase_receptionist"]

# All tool names the agent can use (must match tool .name or function name)
ALL_TOOL_NAMES = [
    "search_faq",
    "create_booking",
    "list_available_slots",
    "calculate",
    "get_current_datetime",
    "get_reference_info",
    "list_supabase_tables",
    "build_supabase_query",
    "execute_supabase_query",
    "search_emails_tool",
    "get_email_content_tool",
    "parse_email_attachment_tool",
    "get_google_doc_text_tool",
]


def get_prompt_override(prompt_key: str) -> Optional[str]:
    """Return override text for the given prompt_key from data/prompt_overrides/{key}.txt, or None."""
    if prompt_key not in ALL_PROMPT_KEYS:
        return None
    path = PROMPT_OVERRIDES_DIR / f"{prompt_key}.txt"
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    except Exception:
        return None


def set_prompt_override(prompt_key: str, content: Optional[str]) -> None:
    """Write or clear the override file for prompt_key. None or empty string deletes the file."""
    if prompt_key not in ALL_PROMPT_KEYS:
        return
    path = PROMPT_OVERRIDES_DIR / f"{prompt_key}.txt"
    if not content or not content.strip():
        if path.exists():
            path.unlink()
        return
    PROMPT_OVERRIDES_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip(), encoding="utf-8")


def load_config() -> Dict[str, Any]:
    """Load workspace config. Returns dict with prompt_key, enabled_tools, welcome_message.
    Override text is stored per-agent in data/prompt_overrides/{prompt_key}.txt (see get_prompt_override).
    Migrates legacy system_prompt from JSON into override file once if present."""
    if not CONFIG_PATH.exists():
        return {"prompt_key": "receptionist", "enabled_tools": None, "welcome_message": None}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        pk = data.get("prompt_key")
        if pk not in ALL_PROMPT_KEYS:
            pk = "receptionist"
        # One-time migration: if JSON had system_prompt and this agent has no override file, write it
        legacy = data.get("system_prompt")
        if legacy and isinstance(legacy, str) and legacy.strip() and get_prompt_override(pk) is None:
            set_prompt_override(pk, legacy.strip())
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"prompt_key": pk, "enabled_tools": data.get("enabled_tools"), "welcome_message": data.get("welcome_message")}, f, indent=2, ensure_ascii=False)
        return {
            "prompt_key": pk,
            "enabled_tools": data.get("enabled_tools"),
            "welcome_message": data.get("welcome_message"),
        }
    except Exception:
        return {"prompt_key": "receptionist", "enabled_tools": None, "welcome_message": None}


def save_config(
    prompt_key: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    welcome_message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update and persist workspace config (prompt_key, enabled_tools, welcome_message).
    Use set_prompt_override(prompt_key, content) to write per-agent override text.
    """
    current = load_config()
    if prompt_key is not None and prompt_key in ALL_PROMPT_KEYS:
        current["prompt_key"] = prompt_key
    if enabled_tools is not None:
        current["enabled_tools"] = [t for t in enabled_tools if t in ALL_TOOL_NAMES] or None
    if welcome_message is not None:
        current["welcome_message"] = welcome_message.strip() or None

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


def patch_config(
    prompt_key: Optional[str] = None,
    system_prompt_override: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    welcome_message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply a full patch from the API. Updates prompt_key, optional per-agent override file,
    enabled_tools, welcome_message. system_prompt_override: None = leave override unchanged;
    empty string = clear override file for current agent; non-empty = write override file.
    """
    current = load_config()
    if prompt_key is not None:
        current["prompt_key"] = prompt_key if prompt_key in ALL_PROMPT_KEYS else current.get("prompt_key", "receptionist")
    pk = current["prompt_key"]
    if system_prompt_override is not None:
        set_prompt_override(pk, system_prompt_override.strip() if system_prompt_override else None)
    current["enabled_tools"] = (
        ([t for t in enabled_tools if t in ALL_TOOL_NAMES] or None) if enabled_tools is not None
        else current.get("enabled_tools")
    )
    current["welcome_message"] = (welcome_message.strip() or None) if welcome_message is not None else current.get("welcome_message")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


def config_hash(config: Dict[str, Any]) -> str:
    """Stable string for cache invalidation (prompt_key + override file content + tools + welcome)."""
    pk = config.get("prompt_key") or "receptionist"
    prompt = (get_prompt_override(pk) or "")[:200]
    tools = ",".join(sorted(config.get("enabled_tools") or ALL_TOOL_NAMES))
    welcome = config.get("welcome_message") or ""
    return f"{pk}|{prompt}|{tools}|{welcome}"
