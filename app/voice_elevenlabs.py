# voice_elevenlabs.py — ElevenLabs TTS and STT for the Live Connect voice experience.
# Provides welcome phrase (time-based), text-to-speech, and speech-to-text.

import os
from datetime import datetime
from typing import Optional

_elevenlabs_client = None


def _get_client():
    """Lazy-init ElevenLabs client. Returns None if ELEVENLABS_API_KEY not set."""
    global _elevenlabs_client
    if _elevenlabs_client is not None:
        return _elevenlabs_client
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        return None
    try:
        from elevenlabs.client import ElevenLabs
        _elevenlabs_client = ElevenLabs(api_key=api_key)
        return _elevenlabs_client
    except Exception:
        return None


def get_welcome_phrase() -> str:
    """Return a time-based welcome phrase for Live Connect (e.g. 'Good morning, how can I help you?')."""
    hour = datetime.now().hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"
    return f"{greeting}, how can I help you?"


def text_to_speech(text: str, voice_id: Optional[str] = None) -> Optional[bytes]:
    """
    Convert text to speech using ElevenLabs. Returns MP3 bytes or None if unavailable.
    voice_id: optional; defaults to ELEVENLABS_VOICE_ID or a default voice.
    """
    client = _get_client()
    if not client or not text.strip():
        return None
    voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # Rachel default
    try:
        # SDK: client.text_to_speech.convert(...) returns bytes
        audio = client.text_to_speech.convert(
            voice_id=voice_id,
            text=text.strip(),
            model_id=os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2"),
        )
        if hasattr(audio, "read"):
            return audio.read()
        if isinstance(audio, bytes):
            return audio
        return bytes(audio)
    except Exception:
        return None


def speech_to_text(audio_bytes: bytes, content_type: str = "audio/webm") -> Optional[str]:
    """
    Convert speech audio to text using ElevenLabs Scribe. Returns transcript or None.
    content_type: e.g. audio/webm, audio/mpeg.
    """
    client = _get_client()
    if not client or not audio_bytes:
        return None
    try:
        from io import BytesIO
        f = BytesIO(audio_bytes)
        result = client.speech_to_text.convert(
            file=f,
            model_id=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"),
            language_code=os.getenv("ELEVENLABS_STT_LANGUAGE", "en"),
        )
        if hasattr(result, "text"):
            return result.text
        if isinstance(result, str):
            return result
        return None
    except Exception:
        return None
