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
    """
    Transcribe a base64-encoded voice note using Groq Whisper.
    Passes (filename, bytes) tuple directly — no temp file needed.
    """
    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY is not configured.")

    from groq import Groq
    client = Groq(api_key=settings.groq_api_key)

    ext = _ext(mime_type)
    audio_bytes = base64.b64decode(audio_data)

    logger.info(f"[VOICE] Received {len(audio_bytes)} bytes, mime={mime_type}, ext=.{ext}")
    logger.info(f"[VOICE] GROQ_API_KEY exists: {bool(settings.groq_api_key)}")
    logger.info("[VOICE] Calling Groq Whisper (bytes tuple, no temp file)...")

    try:
        # Pass (filename, bytes) tuple — avoids all temp-file / fd issues
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3",
            file=(f"audio.{ext}", audio_bytes),
            language="he",
        )
        result = transcription.text.strip()
        logger.info(f"[VOICE] Transcription: \"{result}\"")
        return result

    except Exception as e:
        logger.error(f"[VOICE] Error: {type(e).__name__}: {e}")
        logger.error(f"[VOICE] Full traceback:\n{traceback.format_exc()}")
        raise
