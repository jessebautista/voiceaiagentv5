# workspace_config.py — Read/write workspace settings (system prompt override,
# enabled tools, welcome message). Stored in data/workspace_config.json so the
# workspace UI and the agent share one source of truth. Null/missing = use defaults.

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path(__file__).resolve().parent.parent / "data" / "workspace_config.json"

# All tool names the agent can use (must match tool .name or function name)
ALL_TOOL_NAMES = [
    "search_faq",
    "create_booking",
    "list_available_slots",
    "calculate",
    "get_current_datetime",
    "get_reference_info",
]


def load_config() -> Dict[str, Any]:
    """Load workspace config. Returns dict with system_prompt, enabled_tools, welcome_message (any may be None)."""
    if not CONFIG_PATH.exists():
        return {"system_prompt": None, "enabled_tools": None, "welcome_message": None}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "system_prompt": data.get("system_prompt"),
            "enabled_tools": data.get("enabled_tools"),
            "welcome_message": data.get("welcome_message"),
        }
    except Exception:
        return {"system_prompt": None, "enabled_tools": None, "welcome_message": None}


def save_config(
    system_prompt: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    welcome_message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Update and persist workspace config. Pass None for a key to leave it unchanged.
    Returns the new full config.
    """
    current = load_config()
    if system_prompt is not None:
        current["system_prompt"] = system_prompt.strip() or None
    if enabled_tools is not None:
        current["enabled_tools"] = [t for t in enabled_tools if t in ALL_TOOL_NAMES] or None
    if welcome_message is not None:
        current["welcome_message"] = welcome_message.strip() or None

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


def patch_config(
    system_prompt: Optional[str] = None,
    enabled_tools: Optional[List[str]] = None,
    welcome_message: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Apply a full patch from the API. None for a field means "clear override" (use file/default).
    Empty string also means clear. Returns the new full config.
    """
    current = load_config()
    current["system_prompt"] = (system_prompt.strip() or None) if system_prompt is not None else None
    current["enabled_tools"] = (
        ([t for t in enabled_tools if t in ALL_TOOL_NAMES] or None) if enabled_tools is not None
        else None
    )
    current["welcome_message"] = (welcome_message.strip() or None) if welcome_message is not None else None

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, ensure_ascii=False)
    return current


def config_hash(config: Dict[str, Any]) -> str:
    """Stable string for cache invalidation (prompt + sorted tools + welcome)."""
    prompt = (config.get("system_prompt") or "")[:200]
    tools = ",".join(sorted(config.get("enabled_tools") or ALL_TOOL_NAMES))
    welcome = config.get("welcome_message") or ""
    return f"{prompt}|{tools}|{welcome}"
