# recall.py — Cost-controlled memory recall: Retrieve relevant episodic and procedural
# memories with token budgeting. Prioritizes most relevant memories to stay within limits.

import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from app.memory.episodic import (
    get_facts,
    get_preferences,
    get_recent_events,
    extract_user_id,
)
from app.memory.procedural import get_relevant_procedures

# Editable memory instructions (data/memory/agent_instructions.txt) — how the agent should use recalled context
_MEMORY_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "memory"
AGENT_INSTRUCTIONS_PATH = _MEMORY_DIR / "agent_instructions.txt"


def _load_memory_instructions() -> str:
    """Load optional agent instructions from data/memory/agent_instructions.txt. Skip comment lines."""
    if not AGENT_INSTRUCTIONS_PATH.exists():
        return ""
    try:
        text = AGENT_INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
        # Skip lines that are purely comments
        lines = [line for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        return "\n".join(lines).strip()
    except Exception:
        return ""


def _estimate_tokens(text: str) -> int:
    """Rough token estimation: ~4 chars per token."""
    return len(text) // 4


def _format_memory_section(title: str, items: List[Any], max_tokens: int) -> tuple[str, int]:
    """
    Format a memory section (facts, preferences, etc.) with token limit.
    Returns (formatted_string, tokens_used).
    """
    if not items:
        return "", 0
    
    lines = [f"{title}:"]
    tokens_used = _estimate_tokens(title) + 2
    
    for item in items:
        if isinstance(item, dict):
            if "fact" in item:
                line = f"- {item['fact']}"
            elif "event" in item:
                line = f"- {item['event']}"
            elif "name" in item:
                line = f"- {item['name']} (success rate: {item.get('success_rate', 0):.1%})"
            else:
                line = f"- {json.dumps(item)}"
        else:
            line = f"- {str(item)}"
        
        line_tokens = _estimate_tokens(line)
        if tokens_used + line_tokens > max_tokens:
            break
        lines.append(line)
        tokens_used += line_tokens + 1  # +1 for newline
    
    return "\n".join(lines), tokens_used


def recall_memories(
    session_id: str,
    user_message: str,
    max_tokens: int = 500,
    include_preferences: bool = True,
    include_facts: bool = True,
    include_events: bool = True,
    include_procedures: bool = True,
) -> str:
    """
    Recall relevant memories for a user/session with cost control.
    Returns formatted memory context string, staying within max_tokens.
    
    Token budget allocation (adjustable):
    - Preferences: ~50 tokens (always include if available)
    - Recent facts: ~200 tokens
    - Recent events: ~150 tokens
    - Relevant procedures: ~100 tokens
    """
    user_id = extract_user_id(session_id)
    sections = []
    tokens_remaining = max_tokens
    
    # 1. User preferences (high priority, ~50 tokens)
    if include_preferences and tokens_remaining > 50:
        prefs = get_preferences(user_id)
        if prefs:
            # Filter out internal keys
            display_prefs = {k: v for k, v in prefs.items() if not k.startswith("_")}
            if display_prefs:
                pref_text = "User preferences: " + ", ".join(
                    f"{k}={v}" for k, v in display_prefs.items()
                )
                pref_tokens = _estimate_tokens(pref_text)
                if pref_tokens <= tokens_remaining:
                    sections.append(pref_text)
                    tokens_remaining -= pref_tokens
    
    # 2. Recent relevant facts (~200 tokens); prioritize "User instruction" facts so they are applied
    if include_facts and tokens_remaining > 50:
        all_facts = get_facts(user_id, limit=50)
        user_words = set(user_message.lower().split())
        instruction_facts = []
        other_facts = []
        for fact_entry in all_facts:
            fact_text = fact_entry.get("fact", "")
            fact_words = set(fact_text.lower().split())
            if not (user_words & fact_words):
                continue
            if fact_text.strip().startswith("User instruction:"):
                instruction_facts.append(fact_entry)
            else:
                other_facts.append(fact_entry)
        # Prioritize user instructions so the agent applies them (e.g. "add 2026 in program title")
        relevant_facts = instruction_facts[:5] + other_facts[:7]
        relevant_facts = relevant_facts[:10]
        if relevant_facts:
            fact_budget = min(200, tokens_remaining)
            fact_text, fact_tokens = _format_memory_section("Relevant facts", relevant_facts, fact_budget)
            if fact_text:
                sections.append(fact_text)
                tokens_remaining -= fact_tokens
    
    # 3. Recent events (~150 tokens)
    if include_events and tokens_remaining > 50:
        events = get_recent_events(user_id, limit=20)
        # Filter for relevant events
        user_words = set(user_message.lower().split())
        relevant_events = []
        for event_entry in events:
            event_text = event_entry.get("event", "")
            event_words = set(event_text.lower().split())
            if user_words & event_words:
                relevant_events.append(event_entry)
        
        relevant_events = relevant_events[:5]
        if relevant_events:
            event_budget = min(150, tokens_remaining)
            event_text, event_tokens = _format_memory_section("Recent relevant events", relevant_events, event_budget)
            if event_text:
                sections.append(event_text)
                tokens_remaining -= event_tokens
    
    # 4. Relevant procedures (~100 tokens)
    if include_procedures and tokens_remaining > 50:
        procedures = get_relevant_procedures(user_message, user_id, limit=3)
        if procedures:
            proc_budget = min(100, tokens_remaining)
            proc_text, proc_tokens = _format_memory_section("Learned procedures", procedures, proc_budget)
            if proc_text:
                sections.append(proc_text)
                tokens_remaining -= proc_tokens
    
    if not sections:
        return ""
    
    return "\n\n".join(sections)


def recall_memories_for_agent(
    session_id: str,
    user_message: str,
    max_tokens: int = 500,
) -> str:
    """
    Wrapper for agent integration. Returns memory context to inject into prompt,
    or empty string if no memories or Redis unavailable. Prepends content from
    data/memory/agent_instructions.txt when present so the agent knows how to use context.
    """
    try:
        memory_instructions = _load_memory_instructions()
        context = recall_memories(session_id, user_message, max_tokens=max_tokens)
        parts = []
        if memory_instructions:
            parts.append(memory_instructions)
        if context:
            parts.append(f"Relevant context from past interactions:\n{context}")
        if not parts:
            return ""
        return "\n\n" + "\n\n".join(parts) + "\n"
    except Exception:
        return ""
