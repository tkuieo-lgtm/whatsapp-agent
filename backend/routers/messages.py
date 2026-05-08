import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import select

from config import settings
from database import ActionLog, AsyncSessionLocal, GroupInteraction, PendingAction
from models.schemas import IncomingMessage
from services import whatsapp_service
from services.claude_service import execute_approved_action, process_message

router = APIRouter()
logger = logging.getLogger(__name__)

_YES = {"כן", "yes", "y", "אישור", "ok", "אוקיי"}
_NO = {"לא", "no", "n", "ביטול", "cancel"}


async def _handle_message(msg: IncomingMessage) -> None:
    # --- Voice note: transcribe first ---
    text = msg.message.strip()
    if msg.message_type == "audio" and msg.media_data:
        try:
            from services.voice_service import transcribe_voice
            text = await transcribe_voice(msg.media_data, msg.media_mime or "audio/ogg")
            logger.info(f"[VOICE] Transcribed: {text[:80]}")
            await whatsapp_service.send_message(f"🎤 *תמלול:* {text}")
        except Exception as e:
            logger.error(f"[VOICE] Transcription failed: {e}")
            await whatsapp_service.send_message("❌ לא הצלחתי לתמלל את ההודעה הקולית.")
            return

    # Determine where to send the reply
    reply_chat_id = msg.group_id if msg.is_group else None

    # --- Approval flow (DM only — groups don't have pending actions) ---
    if not msg.is_group and text.lower() in (_YES | _NO):
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

                if text.lower() in _YES:
                    pending.status = "approved"
                    action_type, payload = pending.type, pending.payload
                    await session.commit()
                    response_text = await execute_approved_action(action_type, payload)
                    async with AsyncSessionLocal() as ls:
                        ls.add(ActionLog(action_type=action_type, details=payload, status="approved"))
                        await ls.commit()
                else:
                    pending.status = "rejected"
                    await session.commit()
                    response_text = "✅ הפעולה בוטלה. איך אוכל לעזור?"

                await whatsapp_service.send_message(response_text, chat_id=reply_chat_id)
                return

    # --- Process via Claude ---
    try:
        response_text = await process_message(
            text,
            is_group=msg.is_group,
            group_sender=msg.group_sender,
        )
        await whatsapp_service.send_message(response_text, chat_id=reply_chat_id)

        # Log group interactions separately
        if msg.is_group:
            async with AsyncSessionLocal() as session:
                session.add(GroupInteraction(
                    group_id=msg.group_id or "",
                    sender=msg.group_sender or msg.sender,
                    message=text,
                    response=response_text,
                ))
                await session.commit()

    except Exception as e:
        logger.error(f"[MESSAGES] Error: {e}")
        await whatsapp_service.send_message("❌ אירעה שגיאה. אנא נסה שוב.", chat_id=reply_chat_id)


@router.post("/webhook/message")
async def receive_message(msg: IncomingMessage, background_tasks: BackgroundTasks):
    """Endpoint called by the WhatsApp bridge."""
    # Security: only accept messages from the owner's number
    if msg.sender.replace("+", "") != settings.owner_phone:
        logger.warning(f"[SECURITY] Ignored message from: {msg.sender}")
        return JSONResponse(status_code=200, content={"ok": True})

    background_tasks.add_task(_handle_message, msg)
    return {"ok": True}
