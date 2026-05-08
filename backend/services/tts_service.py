import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)


async def text_to_speech(text: str) -> bytes:
    """Generate speech from text using OpenAI TTS. Returns raw audio bytes (opus)."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is not configured.")

    import openai
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)
    logger.info(f"[TTS] Generating speech for {len(text)} chars…")
    response = await client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text,
        response_format="opus",
    )
    audio_bytes = response.content
    logger.info(f"[TTS] Generated {len(audio_bytes)} bytes")
    return audio_bytes


def should_use_voice(
    text: str,
    was_voice_input: bool = False,
    context_type: str = "text",
) -> bool:
    """Decide whether to send a voice note or a text message."""
    # Always mirror voice with voice
    if was_voice_input:
        return True

    # Scheduled proactive messages → voice
    if context_type in ("morning_briefing", "reminder", "alert"):
        return True

    # Long responses with structure → text is clearer
    word_count = len(text.split())
    has_list = "\n•" in text or "\n-" in text or text.count("\n") > 4
    has_structured_data = any(c in text for c in ["📅", "📧", "🔍", "```"])

    if word_count > 100 or has_list or has_structured_data:
        return False

    # Short conversational reply → voice
    return word_count <= 50
