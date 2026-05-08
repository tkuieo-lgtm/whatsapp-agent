import base64
import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def send_message(message: str, chat_id: Optional[str] = None) -> bool:
    """Send a text WhatsApp message. chat_id is a full JID (e.g. @c.us or @g.us)."""
    try:
        payload: dict = {"message": message}
        if chat_id:
            payload["chat_id"] = chat_id
        else:
            payload["phone"] = settings.owner_phone

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{settings.whatsapp_service_url}/send", json=payload)
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"[WHATSAPP] Failed to send text: {e}")
        return False


async def send_voice_message(text: str, chat_id: Optional[str] = None, context_type: str = "text") -> bool:
    """Generate TTS via gTTS and send as a WhatsApp voice note (mp3)."""
    try:
        from services.tts_service import text_to_speech
        audio_bytes = await text_to_speech(text)
        audio_b64 = base64.b64encode(audio_bytes).decode()

        target = chat_id or f"{settings.owner_phone}@c.us"
        payload: dict = {"to": target, "audio": audio_b64, "mime": "audio/mpeg"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.whatsapp_service_url}/send-voice", json=payload
            )
            resp.raise_for_status()
            logger.info("[TTS] Voice note sent")
            return True

    except Exception as e:
        logger.error(f"[TTS] Voice send failed: {type(e).__name__}: {e} — falling back to text")
        return await send_message(text, chat_id=chat_id)
