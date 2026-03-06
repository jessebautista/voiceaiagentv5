# supabase_client.py — Supabase client factory.
#
# Each agent reads from its own namespaced env vars so they can point
# at completely separate Supabase projects.
#
# Convention in .env:
#   DEV_AGENT_SUPABASE_URL / DEV_AGENT_SUPABASE_SERVICE_KEY
#   VOICE_AGENT_SUPABASE_URL / VOICE_AGENT_SUPABASE_SERVICE_KEY
#   ... etc.
#
# Usage:
#   from app.supabase_client import get_supabase_client
#   supabase = get_supabase_client("DEV_AGENT")   # reads DEV_AGENT_SUPABASE_*
#   supabase = get_supabase_client("VOICE_AGENT")  # reads VOICE_AGENT_SUPABASE_*
#   supabase = get_supabase_client()               # falls back to generic SUPABASE_* (legacy)

import os
from typing import Any, Optional

# Per-agent client cache: { prefix -> client }
_clients: dict[str, Any] = {}


def get_supabase_client(agent_prefix: str = "") -> Optional[Any]:
    """
    Return a Supabase client for the given agent prefix, or None if not configured.

    Args:
        agent_prefix: e.g. "DEV_AGENT" → reads DEV_AGENT_SUPABASE_URL and DEV_AGENT_SUPABASE_SERVICE_KEY.
                      Leave empty to use the legacy generic SUPABASE_URL / SUPABASE_SERVICE_KEY keys.
    """
    cache_key = agent_prefix or "__default__"
    if cache_key in _clients:
        return _clients[cache_key]

    if agent_prefix:
        url = os.getenv(f"{agent_prefix}_SUPABASE_URL", "").strip()
        key = os.getenv(f"{agent_prefix}_SUPABASE_SERVICE_KEY", "").strip()
    else:
        # Legacy fallback — generic keys for backwards compatibility
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip() or os.getenv("SUPABASE_ANON_KEY", "").strip()

    if not url or not key:
        return None

    try:
        from supabase import create_client
        client = create_client(url, key)
        _clients[cache_key] = client
        return client
    except Exception:
        return None


def is_configured(agent_prefix: str = "") -> bool:
    """Return True if the Supabase keys are set for this agent prefix."""
    if agent_prefix:
        url = os.getenv(f"{agent_prefix}_SUPABASE_URL", "").strip()
        key = os.getenv(f"{agent_prefix}_SUPABASE_SERVICE_KEY", "").strip()
    else:
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_KEY", "").strip() or os.getenv("SUPABASE_ANON_KEY", "").strip()
    return bool(url and key)
