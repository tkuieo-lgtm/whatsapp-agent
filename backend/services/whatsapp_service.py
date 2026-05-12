import base64
import logging
import traceback
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evolution API helpers
# ---------------------------------------------------------------------------

def _to_evolution_number(target: str) -> str:
    """Convert JID or phone string to Evolution API number format.

    Groups  → keep full JID  (120363...@g.us)
    DM/phone → digits only   (972546670073)
    """
    if target.endswith("@g.us"):
        return target
    return target.split("@")[0]


async def _ev_send_text(number: str, text: str) -> None:
    url = (
        f"{settings.evolution_api_url.rstrip('/')}"
        f"/message/sendText/{settings.evolution_instance}"
    )
    payload = {
        "number": number,
        "options": {"delay": 1200, "presence": "composing"},
        "textMessage": {"text": text},
    }
    headers = {"apikey": settings.evolution_api_key or ""}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    logger.info(f"[EVOLUTION] Text sent to {number}")


async def _ev_send_audio(number: str, audio_b64: str) -> None:
    url = (
        f"{settings.evolution_api_url.rstrip('/')}"
        f"/message/sendWhatsAppAudio/{settings.evolution_instance}"
    )
    # Evolution API v2 flat format — audio/encoding at top level, no audioMessage wrapper
    payload = {
        "number": number,
        "audio": audio_b64,
        "encoding": True,
    }
    headers = {"apikey": settings.evolution_api_key or ""}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            logger.error(f"[EVOLUTION] sendWhatsAppAudio {resp.status_code}: {resp.text[:300]}")
        resp.raise_for_status()
    logger.info(f"[EVOLUTION] Audio sent to {number} ({len(audio_b64)} b64 chars)")


# ---------------------------------------------------------------------------
# Public API — Evolution API when configured, Baileys bridge as fallback
# ---------------------------------------------------------------------------

async def send_message(message: str, chat_id: Optional[str] = None) -> bool:
    """Send a text WhatsApp message."""
    target = chat_id or f"{settings.owner_phone}@c.us"

    if settings.evolution_api_url:
        try:
            await _ev_send_text(_to_evolution_number(target), message)
            return True
        except Exception as e:
            logger.error(f"[WHATSAPP] Evolution send_text failed: {e}")
            return False

    # Fallback: Baileys bridge
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
        logger.error(f"[WHATSAPP] Baileys send failed: {e}")
        return False


async def send_voice_message(
    text: str,
    chat_id: Optional[str] = None,
    context_type: str = "text",
) -> bool:
    """Generate TTS and send as a WhatsApp voice note (ogg/opus)."""
    try:
        from services.tts_service import text_to_speech
        audio_bytes = await text_to_speech(text)
        logger.info(f"[TTS] Generated {len(audio_bytes)} bytes ogg/opus")
    except Exception as e:
        logger.error(f"[TTS] FAILED at TTS generation: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return False

    audio_b64 = base64.b64encode(audio_bytes).decode()
    target    = chat_id or f"{settings.owner_phone}@c.us"

    if settings.evolution_api_url:
        try:
            await _ev_send_audio(_to_evolution_number(target), audio_b64)
            return True
        except Exception as e:
            logger.error(f"[WHATSAPP] Evolution send_audio failed: {e}")
            return False

    # Fallback: Baileys bridge
    payload = {"to": target, "audio": audio_b64, "mime": "audio/ogg; codecs=opus"}
    logger.info(f"[TTS] Sending voice note to {target} ({len(audio_bytes)} bytes)")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.whatsapp_service_url}/send-voice", json=payload
            )
            if resp.status_code != 200:
                logger.error(f"[TTS] /send-voice returned {resp.status_code}: {resp.text[:300]}")
                return False
        logger.info(f"[TTS] Voice note sent OK")
        return True
    except Exception as e:
        logger.error(f"[TTS] FAILED at /send-voice: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        return False
