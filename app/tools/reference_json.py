# reference_json.py — Parses and queries a reference JSON file (e.g. data/reference.json)
# so the agent can answer policy, company, or config questions from structured data.

import json
from pathlib import Path
from langchain_core.tools import tool

# Path to reference file (project root / data / reference.json)
REFERENCE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "reference.json"


def _load_reference() -> dict:
    """Load reference JSON. Returns {} if missing or invalid."""
    if not REFERENCE_PATH.exists():
        return {}
    try:
        with open(REFERENCE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


@tool
def get_reference_info(query: str = "") -> str:
    """
    Look up information from the reference JSON file (policies, company info, etc.).
    query: optional key path (e.g. "policies.refund_days" or "company.name"). If empty, returns a short summary of top-level keys.
    Use when the user asks about company policies, contact details, or document types accepted.
    """
    data = _load_reference()
    if not data:
        return "Reference file not found or empty."
    query = (query or "").strip()
    if not query:
        keys = list(data.keys())
        return f"Reference contains: {', '.join(keys)}. Ask for a specific key (e.g. policies, company) or use query like 'policies.refund_days'."
    parts = [p.strip() for p in query.split(".") if p.strip()]
    current = data
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return f"Key '{query}' not found in reference."
    if isinstance(current, (dict, list)):
        return json.dumps(current, indent=2)
    return str(current)
