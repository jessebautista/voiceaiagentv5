# query_processor.py — Executes a query spec produced by build_supabase_query against
# Supabase (select, update, insert). If the spec has table_key but no table, the
# table name is resolved from tables_config so the LLM can pass a minimal spec.
# Returns result rows or an error message.

import json
from typing import Any, Dict

from langchain_core.tools import tool

from app.supabase_client import get_supabase_client
from app.tables_config import get_table_config


@tool
def execute_supabase_query(query_spec: str) -> str:
    """
    Execute a Supabase query spec produced by build_supabase_query. Use this after
    build_supabase_query returns a spec: pass that JSON string here to run the find,
    update, or create. query_spec: JSON string with table_key, operation, filters,
    updates (for update), or insert (for create). Returns result rows or an error message.
    """
    client = get_supabase_client()
    if not client:
        return "Supabase is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_KEY in .env."

    try:
        spec = json.loads(query_spec) if isinstance(query_spec, str) else query_spec
    except json.JSONDecodeError as e:
        return f"Invalid query spec JSON: {e}"

    if not isinstance(spec, dict):
        return "Query spec must be a JSON object."

    err = spec.get("error")
    if err:
        return err

    table_key = spec.get("table_key")
    table_name = spec.get("table")
    # LLM often passes only table_key; resolve table name from config if missing
    if not table_name and table_key:
        cfg = get_table_config(table_key)
        if cfg:
            table_name = cfg.get("table")
    operation = (spec.get("operation") or "").strip().lower()
    filters = spec.get("filters") or {}
    updates = spec.get("updates") or {}
    insert = spec.get("insert") or {}

    if not table_name:
        return "Query spec missing 'table' or valid 'table_key' (Supabase table name)."

    try:
        if operation == "find":
            q = client.table(table_name).select("*")
            for col, val in filters.items():
                q = q.eq(col, val)
            r = q.execute()
            return json.dumps({"rows": r.data, "count": len(r.data)})

        if operation == "update":
            if not filters:
                return "Update requires filters to identify the record."
            q = client.table(table_name).update(updates)
            for col, val in filters.items():
                q = q.eq(col, val)
            r = q.execute()
            return json.dumps({"updated": r.data, "count": len(r.data)})

        if operation == "create":
            r = client.table(table_name).insert(insert).execute()
            return json.dumps({"inserted": r.data, "count": len(r.data) if r.data else 0})

        return f"Unknown operation: {operation}. Use find, update, or create."
    except Exception as e:
        return f"Supabase error: {e}"
