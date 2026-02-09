# search_faq.py — RAG tool: searches FAQ (in-memory or from Upstash faq:entries).
# Uses word-based matching so compound questions (e.g. "office and parking") match multiple entries.

import json
import re
from langchain_core.tools import tool

# Default in-memory FAQ (same as scripts/seed_upstash.py). Upstash faq:entries overrides when set.
DEFAULT_FAQ_ENTRIES = [
    {"q": "What are your opening hours?", "a": "We are open Monday–Friday 9am–6pm and Saturday 10am–4pm."},
    {"q": "How can I contact support?", "a": "Email support@company.com or call +1 (555) 123-4567."},
    {"q": "Do you offer refunds?", "a": "Yes. Refunds are available within 30 days with receipt."},
    {"q": "Where is the office?", "a": "Our office is at 123 Main Street, Suite 100."},
    {"q": "Do you have parking?", "a": "Yes. Free parking is available in the lot behind the building."},
    {"q": "What payment methods do you accept?", "a": "We accept credit cards, debit cards, and bank transfer."},
]


def _get_faq_entries() -> list:
    """Return FAQ list from Upstash (faq:entries) if available, else in-memory default."""
    try:
        from app.memory.redis_store import get_memory_store
        store = get_memory_store()
        if store:
            raw = store.get("faq:entries")
            if raw is not None:
                s = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                if s:
                    return json.loads(s)
    except Exception:
        pass
    return DEFAULT_FAQ_ENTRIES


def _query_words(text: str) -> set:
    """Extract meaningful words (2+ chars, letters) for matching."""
    lower = (text or "").lower()
    words = set(re.findall(r"[a-z0-9]{2,}", lower))
    # Drop very common words that add noise
    stop = {"the", "and", "for", "you", "your", "can", "how", "what", "where", "when", "there", "this", "that", "with", "are", "is", "do", "does", "have", "has"}
    return words - stop


@tool
def search_faq(query: str) -> str:
    """
    Search the company FAQ for answers. Use this when the user asks about
    policies, hours, contact info, office location, parking, or general company information.
    """
    entries = _get_faq_entries()
    query_w = _query_words(query)
    if not query_w:
        return "No FAQ entries matched your query. Try rephrasing or ask for contact details."
    results = []
    for entry in entries:
        q_text = (entry.get("q") or "") + " " + (entry.get("a") or "")
        entry_w = _query_words(q_text)
        if query_w & entry_w:  # any query word appears in this entry
            results.append(f"Q: {entry['q']}\nA: {entry['a']}")
    if not results:
        return "No FAQ entries matched your query. Try rephrasing or ask for contact details."
    return "\n\n".join(results[:6])
