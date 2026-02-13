# tables_config.py — Loads per-table reference JSON from data/tables/ so the LLM and
# query_builder know which tables exist, which fields to use for lookups, and which
# columns are editable. Add a new .json file here to support another Supabase table.

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

TABLES_DIR = Path(__file__).resolve().parent.parent / "data" / "tables"

# Cache: table key (filename stem) -> config dict
_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load_all() -> Dict[str, Dict[str, Any]]:
    """Load all table configs from data/tables/*.json."""
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    if not TABLES_DIR.exists():
        return _cache
    for path in TABLES_DIR.glob("*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = path.stem
            if isinstance(data, dict) and data.get("table"):
                _cache[key] = data
        except Exception:
            continue
    return _cache


def list_tables() -> List[str]:
    """Return list of table keys (e.g. news, piano_applications, piano_activations)."""
    return list(_load_all().keys())


def get_table_config(table_key: str) -> Optional[Dict[str, Any]]:
    """Return config for a table by key (filename stem), or None if not found."""
    return _load_all().get(table_key)


def get_tables_schema_for_llm() -> str:
    """Return a concise summary of all tables and lookup fields for the LLM."""
    all_ = _load_all()
    if not all_:
        return "No table configs found in data/tables/."
    lines = []
    for key, cfg in all_.items():
        table = cfg.get("table", key)
        desc = cfg.get("description", "")
        lookup = cfg.get("lookup_fields", [])
        editable = [c for c, meta in cfg.get("columns", {}).items() if meta.get("editable")]
        lines.append(
            f"- {key} (table: {table}): {desc} Lookup by: {', '.join(lookup)}. Editable: {', '.join(editable)}."
        )
    return "\n".join(lines)
