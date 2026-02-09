# booking.py — Calendar/CRM integration: list available slots and create a booking.
# Uses in-memory mock slots for demo; replace with real calendar or CRM API.

from datetime import datetime, timedelta
from langchain_core.tools import tool

# Mock available slots (in production, fetch from calendar/CRM)
MOCK_SLOTS = [
    "2025-02-10 10:00",
    "2025-02-10 14:00",
    "2025-02-11 09:00",
    "2025-02-11 11:00",
    "2025-02-12 15:00",
]
BOOKINGS: list[dict] = []  # In-memory store for demo


@tool
def list_available_slots(date: str = "") -> str:
    """
    List available appointment slots. Optionally pass a date (YYYY-MM-DD).
    Use this before creating a booking so the user can choose a time.
    """
    if date:
        filtered = [s for s in MOCK_SLOTS if s.startswith(date)]
        if not filtered:
            return f"No slots found for {date}. Try another date or ask for 'all slots'."
        return "Available slots: " + ", ".join(filtered)
    return "Available slots: " + ", ".join(MOCK_SLOTS)


@tool
def create_booking(slot: str, name: str, notes: str = "") -> str:
    """
    Create a booking for a given slot. slot format: YYYY-MM-DD HH:MM.
    Use after the user has confirmed they want to book that slot.
    """
    if slot not in MOCK_SLOTS:
        return f"Slot '{slot}' is not available. Use list_available_slots to see options."
    if any(b["slot"] == slot for b in BOOKINGS):
        return f"Slot {slot} is already booked. Please choose another."
    BOOKINGS.append({"slot": slot, "name": name, "notes": notes})
    return f"Booking confirmed for {name} on {slot}. Notes: {notes or 'None'}"
