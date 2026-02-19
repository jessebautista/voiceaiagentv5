# procedural.py — Procedural memory: Track and store learned patterns, workflows,
# and successful procedures. Uses Redis for persistence.

import json
from typing import Dict, Any, Optional, List
from datetime import datetime

from app.memory.redis_store import get_memory_store, _normalize_get


def _procedure_key(name: str) -> str:
    """Generate Redis key for a procedure."""
    return f"procedure:{name}"


def _user_procedure_key(user_id: str, name: str) -> str:
    """Generate Redis key for a user-specific procedure."""
    return f"user:{user_id}:procedure:{name}"


def record_procedure_execution(
    procedure_name: str,
    success: bool,
    steps: Optional[List[str]] = None,
    metadata: Optional[Dict] = None,
    user_id: Optional[str] = None,
) -> bool:
    """
    Record a procedure execution (success or failure) to learn patterns.
    Returns True if recorded successfully.
    """
    store = get_memory_store()
    if not store:
        return False
    try:
        # Update global procedure stats
        proc_key = _procedure_key(procedure_name)
        raw = store.get(proc_key)
        proc_data = {
            "name": procedure_name,
            "total_executions": 0,
            "successful_executions": 0,
            "success_rate": 0.0,
            "common_steps": steps or [],
            "last_execution": datetime.utcnow().isoformat(),
            "metadata": metadata or {},
        }
        if raw:
            s = _normalize_get(raw)
            if s:
                proc_data = json.loads(s)
        
        proc_data["total_executions"] = proc_data.get("total_executions", 0) + 1
        if success:
            proc_data["successful_executions"] = proc_data.get("successful_executions", 0) + 1
        proc_data["success_rate"] = (
            proc_data["successful_executions"] / proc_data["total_executions"]
            if proc_data["total_executions"] > 0
            else 0.0
        )
        if steps:
            # Merge steps (simple: append unique steps)
            existing_steps = proc_data.get("common_steps", [])
            for step in steps:
                if step not in existing_steps:
                    existing_steps.append(step)
            proc_data["common_steps"] = existing_steps[:20]  # Keep top 20 steps
        
        store.set(proc_key, json.dumps(proc_data), ex=86400 * 365)  # 1 year TTL
        
        # Also record user-specific if user_id provided
        if user_id:
            user_proc_key = _user_procedure_key(user_id, procedure_name)
            user_raw = store.get(user_proc_key)
            user_proc_data = proc_data.copy()
            if user_raw:
                s = _normalize_get(user_raw)
                if s:
                    user_proc_data = json.loads(s)
                    user_proc_data["total_executions"] = user_proc_data.get("total_executions", 0) + 1
                    if success:
                        user_proc_data["successful_executions"] = user_proc_data.get("successful_executions", 0) + 1
                    user_proc_data["success_rate"] = (
                        user_proc_data["successful_executions"] / user_proc_data["total_executions"]
                        if user_proc_data["total_executions"] > 0
                        else 0.0
                    )
            else:
                user_proc_data["total_executions"] = 1
                user_proc_data["successful_executions"] = 1 if success else 0
                user_proc_data["success_rate"] = 1.0 if success else 0.0
            
            store.set(user_proc_key, json.dumps(user_proc_data), ex=86400 * 365)
        
        return True
    except Exception:
        return False


def get_procedure(procedure_name: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Retrieve a procedure's learned pattern. Returns user-specific if user_id provided,
    otherwise global procedure.
    """
    store = get_memory_store()
    if not store:
        return None
    try:
        # Try user-specific first if user_id provided
        if user_id:
            user_key = _user_procedure_key(user_id, procedure_name)
            raw = store.get(user_key)
            if raw:
                s = _normalize_get(raw)
                if s:
                    return json.loads(s)
        
        # Fall back to global
        proc_key = _procedure_key(procedure_name)
        raw = store.get(proc_key)
        if raw:
            s = _normalize_get(raw)
            if s:
                return json.loads(s)
    except Exception:
        pass
    return None


def get_relevant_procedures(query: str, user_id: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Find procedures relevant to a query. Simple keyword matching for now.
    Returns list of procedure dicts sorted by relevance.
    """
    store = get_memory_store()
    if not store:
        return []
    
    # Simple keyword matching (can be enhanced with embeddings)
    query_lower = query.lower()
    relevant = []
    
    try:
        # Get all procedure keys (this is a simplified approach)
        # In production, you might maintain an index or use Redis SCAN
        # For now, we'll check common procedure names
        common_procedures = [
            "update_program",
            "create_program",
            "check_program",
            "update_application",
            "check_application",
            "create_news",
            "update_news",
        ]
        
        for proc_name in common_procedures:
            # Check if query mentions procedure keywords
            if any(word in query_lower for word in proc_name.split("_")):
                proc = get_procedure(proc_name, user_id)
                if proc:
                    relevant.append(proc)
        
        # Sort by success rate and recency
        relevant.sort(
            key=lambda x: (
                x.get("success_rate", 0),
                x.get("total_executions", 0),
            ),
            reverse=True,
        )
        
        return relevant[:limit]
    except Exception:
        return []


def infer_procedure_name(user_message: str, tool_calls: List[Dict]) -> Optional[str]:
    """
    Infer procedure name from user message and tool calls.
    Returns procedure name like "update_program" or None.
    """
    message_lower = user_message.lower()
    
    # Check for Supabase operations
    if any("build_supabase_query" in str(tc) for tc in tool_calls):
        if "update" in message_lower:
            if "program" in message_lower or "activation" in message_lower:
                return "update_program"
            if "application" in message_lower:
                return "update_application"
            if "news" in message_lower or "article" in message_lower:
                return "update_news"
        if "create" in message_lower or "add" in message_lower:
            if "program" in message_lower or "activation" in message_lower:
                return "create_program"
            if "application" in message_lower:
                return "create_application"
            if "news" in message_lower or "article" in message_lower:
                return "create_news"
        if "check" in message_lower or "find" in message_lower or "what" in message_lower:
            if "program" in message_lower or "activation" in message_lower:
                return "check_program"
            if "application" in message_lower:
                return "check_application"
    
    return None
