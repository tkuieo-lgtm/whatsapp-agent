import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import select

from config import settings
from database import ActionLog, AsyncSessionLocal, PendingAction
from models.schemas import IncomingMessage
from services import whatsapp_service
from services.claude_service import execute_approved_action, process_message

router = APIRouter()
logger = logging.getLogger(__name__)

_YES = {"כן", "yes", "y", "אישור", "ok", "אוקיי"}
_NO = {"לא", "no", "n", "ביטול", "cancel"}


async def _handle_message(msg: IncomingMessage) -> None:
    text = msg.message.strip()
    lower = text.lower()

    # --- Approval flow ---
    if lower in _YES or lower in _NO:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(PendingAction)
                .where(PendingAction.status == "pending")
                .order_by(PendingAction.created_at.desc())
                .limit(1)
            )
            pending = result.scalar_one_or_none()

            if pending:
                if datetime.now(timezone.utc) > pending.expires_at:
                    pending.status = "expired"
                    await session.commit()
                    await whatsapp_service.send_message("⏰ הפעולה פגה. אנא בקש שוב.")
                    return

                if lower in _YES:
                    pending.status = "approved"
                    action_type = pending.type
                    payload = pending.payload
                    await session.commit()

                    response_text = await execute_approved_action(action_type, payload)

                    async with AsyncSessionLocal() as log_session:
                        log_session.add(ActionLog(
                            action_type=action_type,
                            details=payload,
                            status="approved",
                        ))
                        await log_session.commit()

                    await whatsapp_service.send_message(response_text)
                else:
                    pending.status = "rejected"
                    await session.commit()
                    await whatsapp_service.send_message("✅ הפעולה בוטלה. איך אוכל לעזור?")
                return

    # --- Regular message ---
    try:
        response_text = await process_message(text)
        await whatsapp_service.send_message(response_text)
    except Exception as e:
        logger.error(f"[MESSAGES] Unhandled error: {e}")
        await whatsapp_service.send_message("❌ אירעה שגיאה. אנא נסה שוב.")


@router.post("/webhook/message")
async def receive_message(msg: IncomingMessage, background_tasks: BackgroundTasks):
    """Endpoint called by the WhatsApp bridge when a message arrives."""
    if msg.sender.replace("+", "") != settings.owner_phone:
        logger.warning(f"[SECURITY] Ignored message from unknown sender: {msg.sender}")
        return JSONResponse(status_code=200, content={"ok": True})

    background_tasks.add_task(_handle_message, msg)
    return {"ok": True}
