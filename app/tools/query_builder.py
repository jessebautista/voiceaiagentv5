# query_builder.py — Builds a structured query spec (find/update/create) from table key,
# operation, and field values. Uses data/tables/*.json so only valid lookup and
# editable columns are used. The spec is passed to query_processor to execute.

import json
from typing import Any, Dict, Optional

from langchain_core.tools import tool

from app.tables_config import get_table_config, get_tables_schema_for_llm, list_tables


def _validate_filters(table_cfg: Dict[str, Any], filters: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only filter keys that are in lookup_fields or columns."""
    lookup = set(table_cfg.get("lookup_fields", []))
    cols = set(table_cfg.get("columns", {}).keys())
    allowed = lookup | cols
    return {k: v for k, v in filters.items() if k in allowed}


def _validate_updates(table_cfg: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only editable columns."""
    columns = table_cfg.get("columns", {})
    return {k: v for k, v in updates.items() if columns.get(k, {}).get("editable") is True}


def _validate_insert(table_cfg: Dict[str, Any], insert: Dict[str, Any]) -> Dict[str, Any]:
    """Allow any column that exists; omit id and created_at if not provided."""
    columns = table_cfg.get("columns", {})
    return {k: v for k, v in insert.items() if k in columns}


@tool
def build_supabase_query(
    table_key: str,
    operation: str,
    filters: str = "{}",
    updates: str = "{}",
    insert: str = "{}",
) -> str:
    """
    Build a query spec for Supabase (find, update, or create). Use this when the user
    asks to look up, update, or add a record in news, piano_applications, piano_activations, etc.
    table_key: one of the table keys (e.g. news, piano_applications, piano_activations). Use list_supabase_tables to see available tables.
    operation: "find" (look up by filters), "update" (set fields where filters match), or "create" (insert a new row).
    filters: JSON object of column=value for finding the record (e.g. {"news_title": "My Article"}).
    updates: JSON object of column=value for update operation (only editable columns).
    insert: JSON object of column=value for create operation.
    Returns a query spec string to pass to execute_supabase_query. If Supabase is not configured, returns an error message.
    """
    table_cfg = get_table_config(table_key)
    if not table_cfg:
        available = ", ".join(list_tables()) or "none"
        return json.dumps({"error": f"Unknown table_key: {table_key}. Available: {available}"})

    op = (operation or "").strip().lower()
    if op not in ("find", "update", "create"):
        return json.dumps({"error": "operation must be find, update, or create"})

    try:
        f = json.loads(filters) if isinstance(filters, str) else filters
        u = json.loads(updates) if isinstance(updates, str) else updates
        i = json.loads(insert) if isinstance(insert, str) else insert
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON in filters/updates/insert: {e}"})

    if not isinstance(f, dict):
        f = {}
    if not isinstance(u, dict):
        u = {}
    if not isinstance(i, dict):
        i = {}

    spec = {
        "table_key": table_key,
        "table": table_cfg["table"],
        "operation": op,
        "filters": _validate_filters(table_cfg, f),
        "updates": _validate_updates(table_cfg, u) if op == "update" else {},
        "insert": _validate_insert(table_cfg, i) if op == "create" else {},
    }

    if op == "update" and not spec["filters"]:
        return json.dumps({"error": "update requires at least one filter to identify the record"})
    if op == "create" and not spec["insert"]:
        return json.dumps({"error": "create requires at least one field in insert"})

    return json.dumps(spec)


@tool
def list_supabase_tables() -> str:
    """
    List Supabase tables available for CRUD and their lookup/editable fields. Use this
    when the user asks to add, update, or look up records so you know which table_key
    and fields to pass to build_supabase_query.
    """
    return get_tables_schema_for_llm()
