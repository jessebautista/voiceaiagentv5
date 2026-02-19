# tools/ — Modular skills (the "hands"): search_faq, booking, calculator, datetime, reference, Supabase CRUD.
# Each module exposes LangChain @tool functions for the agent to call.
# Supabase CRUD uses data/tables/*.json and query_builder + query_processor.

from app.tools.search_faq import search_faq
from app.tools.booking import create_booking, list_available_slots
from app.tools.calculator import calculate
from app.tools.current_datetime import get_current_datetime
from app.tools.reference_json import get_reference_info
from app.tools.query_builder import build_supabase_query, list_supabase_tables
from app.tools.query_processor import execute_supabase_query
from app.tools.web_fetcher import fetch_web_page

__all__ = [
    "search_faq",
    "create_booking",
    "list_available_slots",
    "calculate",
    "get_current_datetime",
    "get_reference_info",
    "list_supabase_tables",
    "build_supabase_query",
    "execute_supabase_query",
    "fetch_web_page",
]
