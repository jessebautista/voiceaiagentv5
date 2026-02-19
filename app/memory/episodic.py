# episodic.py — Episodic memory: Extract, store, and retrieve user facts, preferences,
# and past events from conversations. Uses Redis for persistence with user-scoped keys.

import json
import os
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.memory.redis_store import get_memory_store, _normalize_get


def _user_key(user_id: str, suffix: str) -> str:
    """Generate Redis key for user-scoped memory."""
    return f"user:{user_id}:{suffix}"


def _session_key(session_id: str, suffix: str) -> str:
    """Generate Redis key for session-scoped memory."""
    return f"session:{session_id}:{suffix}"


def extract_user_id(session_id: str) -> str:
    """
    Extract user identifier from session_id. For now, uses session_id as user_id.
    In production, you might extract from session metadata or user authentication.
    """
    # If session_id contains user info (e.g. "user-123-session-456"), extract it
    # For now, use session_id as user_id (one user per session)
    return session_id


def store_fact(user_id: str, fact: str, confidence: float = 1.0, metadata: Optional[Dict] = None) -> bool:
    """
    Store an episodic fact (e.g. "user prefers email", "user updated Liberty Station last week").
    Returns True if stored successfully.
    """
    store = get_memory_store()
    if not store:
        return False
    try:
        fact_entry = {
            "fact": fact,
            "confidence": confidence,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        key = _user_key(user_id, "facts")
        # Use Redis list to store facts (can be capped later)
        fact_json = json.dumps(fact_entry)
        store.lpush(key, fact_json)
        store.ltrim(key, 0, 999)  # Keep last 1000 facts
        store.expire(key, 86400 * 90)  # 90 days TTL
        return True
    except Exception:
        return False


def store_preference(user_id: str, key: str, value: Any) -> bool:
    """
    Store a user preference (e.g. {"preferred_contact": "email", "timezone": "EST"}).
    Updates existing preferences dict.
    """
    store = get_memory_store()
    if not store:
        return False
    try:
        pref_key = _user_key(user_id, "preferences")
        raw = store.get(pref_key)
        prefs = {}
        if raw:
            s = _normalize_get(raw)
            if s:
                prefs = json.loads(s)
        prefs[key] = value
        prefs["_updated"] = datetime.utcnow().isoformat()
        store.set(pref_key, json.dumps(prefs), ex=86400 * 365)  # 1 year TTL
        return True
    except Exception:
        return False


def store_event(user_id: str, event: str, details: Optional[Dict] = None) -> bool:
    """
    Store an episodic event (e.g. "updated Liberty Station program", "created booking").
    """
    store = get_memory_store()
    if not store:
        return False
    try:
        event_entry = {
            "event": event,
            "timestamp": datetime.utcnow().isoformat(),
            "details": details or {},
        }
        key = _user_key(user_id, "events")
        event_json = json.dumps(event_entry)
        store.lpush(key, event_json)
        store.ltrim(key, 0, 499)  # Keep last 500 events
        store.expire(key, 86400 * 90)  # 90 days TTL
        return True
    except Exception:
        return False


def get_facts(user_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Retrieve stored facts for a user, most recent first."""
    store = get_memory_store()
    if not store:
        return []
    try:
        key = _user_key(user_id, "facts")
        raw_list = store.lrange(key, 0, limit - 1)
        facts = []
        for raw in raw_list:
            s = _normalize_get(raw)
            if s:
                try:
                    facts.append(json.loads(s))
                except Exception:
                    continue
        return facts
    except Exception:
        return []


def get_preferences(user_id: str) -> Dict[str, Any]:
    """Retrieve user preferences."""
    store = get_memory_store()
    if not store:
        return {}
    try:
        key = _user_key(user_id, "preferences")
        raw = store.get(key)
        if raw:
            s = _normalize_get(raw)
            if s:
                return json.loads(s)
    except Exception:
        pass
    return {}


def get_recent_events(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve recent events for a user."""
    store = get_memory_store()
    if not store:
        return []
    try:
        key = _user_key(user_id, "events")
        raw_list = store.lrange(key, 0, limit - 1)
        events = []
        for raw in raw_list:
            s = _normalize_get(raw)
            if s:
                try:
                    events.append(json.loads(s))
                except Exception:
                    continue
        return events
    except Exception:
        return []


# Phrases that indicate the user is giving an instruction/rule to follow (edit via data/memory/extraction_phrases.txt later if desired)
INSTRUCTION_TRIGGERS = (
    "moving forward",
    "from now on",
    "make sure",
    "whenever we",
    "always ",
    "when we ",
    "when creating",
    "when you create",
    "when adding",
    "we're adding",
    "we are adding",
    "include ",
    "add ",
    " in the program",
    " in the title",
)


def _normalize_instruction(text: str) -> str:
    """Normalize spoken form to a short instruction (e.g. 'twenty twenty-six' -> '2026')."""
    import re
    t = text.strip()
    # Common spoken year forms
    t = re.sub(r"\btwenty twenty-six\b", "2026", t, flags=re.IGNORECASE)
    t = re.sub(r"\btwenty twenty-five\b", "2025", t, flags=re.IGNORECASE)
    t = re.sub(r"\b(\d+)\s+(\d+)\b", lambda m: m.group(1) + m.group(2) if len(m.group(1)) <= 2 and len(m.group(2)) <= 2 else m.group(0), t)  # e.g. "twenty 26" -> "2026" simplified
    return t[:500]  # cap length


def extract_facts_from_conversation(
    session_id: str, user_message: str, agent_response: str, messages: List[Any]
) -> List[str]:
    """
    Extract facts from a conversation turn. Captures preferences, events, and user instructions.
    User instructions (e.g. "add 2026 in the program title when creating programs") are stored
    so they can be recalled and applied in future turns.
    Returns list of fact strings.
    """
    facts = []
    user_id = extract_user_id(session_id)
    msg_lower = user_message.lower()
    
    # User instructions / rules: "moving forward", "make sure that", "whenever we create", etc.
    for trigger in INSTRUCTION_TRIGGERS:
        if trigger.strip() in msg_lower:
            # Store as a clear instruction so recall and agent can apply it
            normalized = _normalize_instruction(user_message)
            instruction = f"User instruction: {normalized}"
            if instruction not in [f for f in facts]:
                facts.append(instruction)
            break  # one instruction per message is enough; avoid duplicates
    
    # Preferences
    if "prefer" in msg_lower or "like" in msg_lower:
        if "email" in msg_lower:
            facts.append("User prefers email communication")
        if "phone" in msg_lower or "call" in msg_lower:
            facts.append("User prefers phone communication")
    
    # Program/record updates
    if "update" in msg_lower or "change" in msg_lower:
        if "liberty station" in msg_lower:
            facts.append("User updated Liberty Station program")
        if "program" in msg_lower:
            facts.append("User updated a program")
    
    # User info
    if "name is" in msg_lower or "i'm" in msg_lower:
        pass
    
    return facts
