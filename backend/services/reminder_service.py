import logging
from datetime import datetime, timezone

from sqlalchemy import select

from database import AsyncSessionLocal, Reminder
from services import whatsapp_service

logger = logging.getLogger(__name__)


async def check_and_send_reminders() -> None:
    """Send any reminders whose time has come. Runs every minute via scheduler."""
    try:
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.sent.is_(False))
                .where(Reminder.remind_at <= now)
            )
            due = result.scalars().all()

            for reminder in due:
                msg = f"🔔 *תזכורת:* {reminder.text}"
                sent = await whatsapp_service.send_message(msg)
                if sent:
                    reminder.sent = True
                    logger.info(f"[REMINDER] Sent: {reminder.text}")
                else:
                    logger.warning(f"[REMINDER] Failed to send: {reminder.text}")

            if due:
                await session.commit()

    except Exception as e:
        logger.error(f"[REMINDER] check_and_send_reminders error: {e}")
