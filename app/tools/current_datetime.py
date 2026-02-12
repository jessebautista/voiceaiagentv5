# current_datetime.py — Tool that returns the current date and time so the agent
# can answer "what day is it?", "what time is it?", or use it when booking or scheduling.

from datetime import datetime
from zoneinfo import ZoneInfo
from langchain_core.tools import tool


@tool
def get_current_datetime(timezone: str = "") -> str:
    """
    Get the current date and time. Use this when the user asks what day it is,
    what time it is, or when scheduling or booking to know "today" or "now".
    timezone: optional IANA name (e.g. America/New_York). If empty, uses system/local time.
    """
    if timezone and timezone.strip():
        try:
            now = datetime.now(ZoneInfo(timezone.strip()))
        except Exception:
            now = datetime.now()
    else:
        now = datetime.now()
    return now.strftime("%A, %B %d, %Y at %I:%M %p")  # e.g. Monday, February 10, 2025 at 10:30 AM
