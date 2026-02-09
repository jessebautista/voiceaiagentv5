# test_agent.py — Basic tests: tools work and agent returns a reply (with mocked LLM if needed).

import pytest
from app.tools.calculator import calculate
from app.tools.search_faq import search_faq
from app.tools.booking import list_available_slots, create_booking
from app.agents.sme_expert import is_sme_query


def test_calculator():
    assert calculate.invoke({"expression": "2 + 3"}) == "5"
    assert calculate.invoke({"expression": "(10 - 2) * 3"}) == "24"


def test_search_faq():
    out = search_faq.invoke({"query": "opening hours"})
    assert "9am" in out or "hours" in out.lower()


def test_list_slots():
    out = list_available_slots.invoke({})
    assert "2025" in out


def test_create_booking():
    out = create_booking.invoke({"slot": "2025-02-10 10:00", "name": "Test User", "notes": ""})
    assert "confirmed" in out.lower() or "Booking" in out


def test_is_sme_query():
    assert is_sme_query("How does the API work?") is True
    assert is_sme_query("What are your hours?") is False
