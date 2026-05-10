import base64
import logging
import traceback

from config import settings

logger = logging.getLogger(__name__)

_MIME_TO_EXT = {
    "ogg": "ogg", "webm": "webm",
    "mp4": "mp4", "m4a": "m4a",
    "mpeg": "mp3", "mp3": "mp3",
}


def _ext(mime_type: str) -> str:
    for key, ext in _MIME_TO_EXT.items():
        if key in mime_type:
            return ext
    return "ogg"


async def transcribe_voice(audio_data: str, mime_type: str = "audio/ogg") -> str:
    """Transcribe a base64-encoded voice note using Groq Whisper."""
    logger.info(f"[VOICE] Step 0: transcribe_voice called, mime={mime_type}")

    if not settings.groq_api_key:
        logger.error("[VOICE] GROQ_API_KEY is not set!")
        raise ValueError("GROQ_API_KEY is not configured.")

    logger.info(f"[VOICE] Step 0b: GROQ_API_KEY present, len={len(settings.groq_api_key)}, prefix={settings.groq_api_key[:10]}...")

    try:
        audio_bytes = base64.b64decode(audio_data)
    except Exception as e:
        logger.error(f"[VOICE] Step 1: base64 decode FAILED: {e}")
        raise

    logger.info(f"[VOICE] Step 1: received audio bytes: {len(audio_bytes)}")

    ext = _ext(mime_type)
    filename = f"audio.{ext}"
    logger.info(f"[VOICE] Step 1b: will send as filename={filename!r}")

    try:
        from groq import Groq
        client = Groq(api_key=settings.groq_api_key)
        logger.info("[VOICE] Step 2: Groq client created OK")
    except Exception as e:
        logger.error(f"[VOICE] Step 2: Groq client creation FAILED: {type(e).__name__}: {e}")
        raise

    logger.info("[VOICE] Step 2b: about to call Groq...")
    try:
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=(filename, audio_bytes),
            language="he",
        )
        result = transcription.text.strip()
        logger.info(f"[VOICE] Step 3: Groq response: {result!r}")
        return result

    except Exception as e:
        logger.error(f"[VOICE] Step 3: Groq API call FAILED: {type(e).__name__}: {e}")
        logger.error(f"[VOICE] Full traceback:\n{traceback.format_exc()}")
        raise
