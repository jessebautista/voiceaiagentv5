# redis_store.py — Saves and loads chat history by session_id using Redis.
# Supports Upstash (UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN) or
# standard Redis (REDIS_URL). If neither is set, returns None and the agent
# uses in-memory checkpointer only.

import os
import json
from typing import Optional, Any

_redis_client: Optional[Any] = None
_using_upstash = False


def get_memory_store():
    """
    Return a Redis client if Upstash or REDIS_URL is set; otherwise None.
    Prefers Upstash REST (UPSTASH_REDIS_REST_URL + UPSTASH_REDIS_REST_TOKEN).
    """
    global _redis_client, _using_upstash
    if _redis_client is not None:
        return _redis_client
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        try:
            from upstash_redis import Redis
            _redis_client = Redis(url=url, token=token)
            _using_upstash = True
            return _redis_client
        except Exception:
            pass
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        try:
            import redis
            _redis_client = redis.from_url(redis_url)
            return _redis_client
        except Exception:
            pass
    return None


def _key(session_id: str) -> str:
    return f"agent:chat:{session_id}"


def _normalize_get(raw: Any) -> Optional[str]:
    """Return string from Redis get (bytes or str)."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8")
    return str(raw)


def save_messages(session_id: str, messages: list) -> bool:
    """Persist message list for session_id. Returns True if Redis is available."""
    store = get_memory_store()
    if not store:
        return False
    try:
        payload = json.dumps([
            {"role": getattr(m, "type", "unknown"), "content": getattr(m, "content", str(m))}
            for m in messages
        ])
        store.set(_key(session_id), payload, ex=86400 * 7)  # 7 days TTL
        return True
    except Exception:
        return False


def load_messages(session_id: str) -> list:
    """Load persisted messages for session_id. Returns [] if not found or no Redis."""
    store = get_memory_store()
    if not store:
        return []
    try:
        raw = store.get(_key(session_id))
        s = _normalize_get(raw)
        if not s:
            return []
        return json.loads(s)
    except Exception:
        return []
