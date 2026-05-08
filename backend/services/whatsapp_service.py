import logging
import httpx
from config import settings

logger = logging.getLogger(__name__)


async def send_message(message: str) -> bool:
    """Send a WhatsApp message to the owner's phone."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{settings.whatsapp_service_url}/send",
                json={"phone": settings.owner_phone, "message": message},
            )
            resp.raise_for_status()
            return True
    except Exception as e:
        logger.error(f"[WHATSAPP] Failed to send message: {e}")
        return False
