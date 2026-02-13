# supabase_client.py — Supabase client for CRUD (news, piano_applications,
# piano_activations, etc.). Uses SUPABASE_URL and SUPABASE_SERVICE_KEY from .env.
# Returns None if not configured so callers can skip DB operations.

import os
from typing import Any, Optional

_client: Optional[Any] = None


def get_supabase_client():
    """Return the Supabase client, or None if SUPABASE_URL or SUPABASE_SERVICE_KEY is not set."""
    global _client
    if _client is not None:
        return _client
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip() or os.getenv("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _client = create_client(url, key)
        return _client
    except Exception:
        return None


def is_configured() -> bool:
    """Return True if Supabase URL and key are set (client may still fail to connect)."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip() or os.getenv("SUPABASE_ANON_KEY", "").strip()
    return bool(url and key)
