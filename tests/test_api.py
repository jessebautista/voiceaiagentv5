# test_api.py — FastAPI endpoints: health and chat (chat may require OPENAI_API_KEY or mock).

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_empty_message():
    r = client.post("/chat", json={"session_id": "test-session", "message": "   "})
    assert r.status_code == 200
    data = r.json()
    assert "reply" in data
    assert "Please send a non-empty" in data["reply"] or data["reply"]


def test_chat_request_body():
    # Valid body; may return 200 with error if OPENAI_API_KEY not set
    r = client.post("/chat", json={"session_id": "test-123", "message": "Hello"})
    assert r.status_code == 200
    data = r.json()
    assert "reply" in data
    assert "error" in data
