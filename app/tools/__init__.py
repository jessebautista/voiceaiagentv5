# tools/ — Modular skills (the "hands"): search_faq, booking, calculator, datetime, reference.
# Each module exposes LangChain @tool functions for the agent to call.
# (Email ingestion / linking to records is handled by a separate system after the call.)

from app.tools.search_faq import search_faq
from app.tools.booking import create_booking, list_available_slots
from app.tools.calculator import calculate
from app.tools.current_datetime import get_current_datetime
from app.tools.reference_json import get_reference_info

__all__ = [
    "search_faq",
    "create_booking",
    "list_available_slots",
    "calculate",
    "get_current_datetime",
    "get_reference_info",
]
