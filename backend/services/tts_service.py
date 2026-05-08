import logging
import os
import tempfile

logger = logging.getLogger(__name__)


async def text_to_speech(text: str) -> bytes:
    """Convert text to speech using gTTS (free, no API key needed). Returns mp3 bytes."""
    from gtts import gTTS

    logger.info(f"[TTS] Generating speech for {len(text)} chars…")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
    os.close(tmp_fd)
    try:
        tts = gTTS(text=text, lang="he")
        tts.save(tmp_path)
        with open(tmp_path, "rb") as f:
            audio_bytes = f.read()
        logger.info(f"[TTS] Generated {len(audio_bytes)} bytes")
        return audio_bytes
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def should_use_voice(
    text: str,
    was_voice_input: bool = False,
    context_type: str = "text",
) -> bool:
    """Decide whether to send a voice note or a text message."""
    if was_voice_input:
        return True
    if context_type in ("morning_briefing", "reminder", "alert"):
        return True
    word_count = len(text.split())
    has_list = "\n•" in text or "\n-" in text or text.count("\n") > 4
    has_structured_data = any(c in text for c in ["📅", "📧", "🔍", "```"])
    if word_count > 100 or has_list or has_structured_data:
        return False
    return word_count <= 50
