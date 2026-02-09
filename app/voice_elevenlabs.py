# voice_elevenlabs.py — ElevenLabs TTS and STT for the Live Connect voice experience.
# Implements .agents/skills/text-to-speech and speech-to-text: use SDK, consume TTS stream.

import logging
import os
from datetime import datetime
from typing import Optional

logger = logging.getLogger("app.voice")

_elevenlabs_client = None


def _get_client():
    """
    Lazy-init ElevenLabs client (per skill: use env ELEVENLABS_API_KEY).
    Returns None if key not set.
    """
    global _elevenlabs_client
    if _elevenlabs_client is not None:
        return _elevenlabs_client
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        logger.warning("ELEVENLABS_API_KEY not set; voice (TTS/STT) will be unavailable.")
        return None
    try:
        from elevenlabs.client import ElevenLabs
        # Per skill: client = ElevenLabs() uses env; or ElevenLabs(api_key=...)
        _elevenlabs_client = ElevenLabs(api_key=api_key)
        return _elevenlabs_client
    except Exception as e:
        logger.exception("ElevenLabs client init failed: %s", e)
        return None


def get_welcome_phrase() -> str:
    """Time-based welcome for Live Connect (e.g. 'Good morning, how can I help you?')."""
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
    Convert text to speech using ElevenLabs SDK (per text-to-speech skill).
    convert() returns an iterator of bytes; we consume it and return full MP3 bytes.
    """
    client = _get_client()
    if not client or not text.strip():
        return None
    voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")  # George per skill
    model_id = os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")
    try:
        # Per skill: audio = client.text_to_speech.convert(text=..., voice_id=..., model_id=...)
        # then: for chunk in audio: f.write(chunk)  → we join chunks into bytes
        audio_stream = client.text_to_speech.convert(
            text=text.strip(),
            voice_id=voice_id,
            model_id=model_id,
        )
        # convert() returns Iterator[bytes]; consume and concatenate
        if hasattr(audio_stream, "__iter__") and not isinstance(audio_stream, (str, bytes)):
            return b"".join(audio_stream)
        if isinstance(audio_stream, bytes):
            return audio_stream
        if hasattr(audio_stream, "read"):
            return audio_stream.read()
        return None
    except Exception as e:
        logger.exception("ElevenLabs TTS failed: %s", e)
        return None


def speech_to_text(audio_bytes: bytes, content_type: str = "audio/webm") -> Optional[str]:
    """
    Transcribe audio to text using ElevenLabs Scribe (per speech-to-text skill).
    client.speech_to_text.convert(file=..., model_id="scribe_v2") → result.text
    """
    client = _get_client()
    if not client or not audio_bytes:
        return None
    try:
        from io import BytesIO
        # Per skill: file=audio_file (file-like), model_id="scribe_v2"
        result = client.speech_to_text.convert(
            file=BytesIO(audio_bytes),
            model_id=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v2"),
            language_code=os.getenv("ELEVENLABS_STT_LANGUAGE", "eng"),
        )
        if hasattr(result, "text"):
            return result.text
        if isinstance(result, str):
            return result
        return None
    except Exception as e:
        logger.exception("ElevenLabs STT failed: %s", e)
        return None
