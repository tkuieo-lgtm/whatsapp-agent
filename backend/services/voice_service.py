import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)


async def transcribe_voice(audio_data: str, mime_type: str = "audio/ogg") -> str:
    """Transcribe a base64-encoded voice note using OpenAI Whisper."""
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is not configured.")

    import openai
    client = openai.AsyncOpenAI(api_key=settings.openai_api_key)

    # Choose file suffix based on MIME type
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

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        with open(tmp_path, "rb") as audio_file:
            transcript = await client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="he",
            )

        logger.info(f"[VOICE] Transcribed: {transcript.text[:80]}")
        return transcript.text

    except Exception as e:
        logger.error(f"[VOICE] Transcription error: {e}")
        raise
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
