import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def send_message(message: str, chat_id: Optional[str] = None) -> bool:
    """
    Send a WhatsApp message.
    - chat_id: full JID (e.g. '972XXXXXXXXX@c.us' or 'XXXXXXXX@g.us').
               If omitted, sends to OWNER_PHONE.
    """
    try:
        payload: dict = {"message": message}
        if chat_id:
            payload["chat_id"] = chat_id
        else:
            payload["phone"] = settings.owner_phone

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.whatsapp_service_url}/send",
                json=payload,
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"[WHATSAPP] Failed to send message: {e}")
        return False
