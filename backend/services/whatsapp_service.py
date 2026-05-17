import base64
import logging
import traceback
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def send_message(message: str, chat_id: Optional[str] = None) -> bool:
    """Send a text WhatsApp message via Baileys bridge."""
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
        logger.error(f"[WHATSAPP] send_message failed: {e}")
        return False


async def send_voice_message(text: str, chat_id: Optional[str] = None) -> bool:
    """Generate TTS and send as a WhatsApp voice note (ogg/opus) via Baileys bridge."""
    try:
        from services.tts_service import text_to_speech
        audio_bytes = await text_to_speech(text)
        logger.info(f"[TTS] Generated {len(audio_bytes)} bytes ogg/opus")
    except Exception as e:
        logger.error(f"[TTS] TTS generation failed: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return False

    audio_b64 = base64.b64encode(audio_bytes).decode()
    target    = chat_id or f"{settings.owner_phone}@c.us"
    payload   = {"to": target, "audio": audio_b64, "mime": "audio/ogg; codecs=opus"}

    logger.info(f"[TTS] Sending voice note to {target} ({len(audio_bytes)} bytes)")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{settings.whatsapp_service_url}/send-voice", json=payload)
            if resp.status_code != 200:
                logger.error(f"[TTS] /send-voice returned {resp.status_code}: {resp.text[:300]}")
                return False
        logger.info("[TTS] Voice note sent OK")
        return True
    except Exception as e:
        logger.error(f"[TTS] /send-voice failed: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return False
