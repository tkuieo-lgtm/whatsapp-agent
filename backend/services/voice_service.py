import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)


async def transcribe_voice(audio_data: str, mime_type: str = "audio/ogg") -> str:
    """Transcribe a base64-encoded voice note using Groq Whisper (free tier)."""
    if not settings.groq_api_key:
        raise ValueError("GROQ_API_KEY is not configured.")

    from groq import Groq
    client = Groq(api_key=settings.groq_api_key)

    if "ogg" in mime_type:
        suffix = ".ogg"
    elif "mp4" in mime_type or "m4a" in mime_type:
        suffix = ".m4a"
    elif "webm" in mime_type:
        suffix = ".webm"
    elif "mpeg" in mime_type or "mp3" in mime_type:
        suffix = ".mp3"
    else:
        suffix = ".ogg"

    audio_bytes = base64.b64decode(audio_data)
    logger.info(f"[VOICE] Received audio, size: {len(audio_bytes)} bytes, mime: {mime_type}")
    logger.info(f"[VOICE] GROQ_API_KEY exists: {bool(settings.groq_api_key)}")

    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(tmp_fd, audio_bytes)
        finally:
            os.close(tmp_fd)

        logger.info(f"[VOICE] Temp file path: {tmp_path}")
        logger.info(f"[VOICE] Audio file size: {os.path.getsize(tmp_path)} bytes")
        logger.info("[VOICE] Calling Groq Whisper...")

        with open(tmp_path, "rb") as audio_file:
            transcription = client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=audio_file,
                language="he",
            )

        result_text = transcription.text.strip()
        logger.info(f"[VOICE] Transcription result: \"{result_text}\"")

        if not result_text:
            return ""   # Caller handles empty string

        return result_text

    except Exception as e:
        logger.error(f"[VOICE] Error: {type(e).__name__}: {str(e)}")
        raise
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
