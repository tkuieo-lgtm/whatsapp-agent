import logging
import os
import tempfile

logger = logging.getLogger(__name__)


async def text_to_speech(text: str) -> bytes:
    """Convert text to OGG Opus via gTTS + pydub (WhatsApp-compatible voice note)."""
    from gtts import gTTS
    from pydub import AudioSegment

    logger.info(f"[TTS] Generating speech for {len(text)} chars…")

    mp3_fd, mp3_path = tempfile.mkstemp(suffix=".mp3")
    ogg_fd, ogg_path = tempfile.mkstemp(suffix=".ogg")
    os.close(mp3_fd)
    os.close(ogg_fd)

    try:
        # Step 1: gTTS → mp3
        gTTS(text=text, lang="iw").save(mp3_path)

        # Step 2: mp3 → ogg/opus (WhatsApp requires this format)
        audio = AudioSegment.from_mp3(mp3_path)
        audio.export(ogg_path, format="ogg", codec="libopus")

        with open(ogg_path, "rb") as f:
            audio_bytes = f.read()

        logger.info(f"[TTS] Generated {len(audio_bytes)} bytes (ogg/opus)")
        return audio_bytes
    finally:
        for p in (mp3_path, ogg_path):
            if os.path.exists(p):
                os.unlink(p)


def should_use_voice(
    text: str,
    was_voice_input: bool = False,
    context_type: str = "text",
) -> bool:
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
