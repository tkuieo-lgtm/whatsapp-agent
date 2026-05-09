import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import select, update

from config import settings
from database import ActionLog, AsyncSessionLocal, GroupInteraction, PendingAction, VoicePreference
from models.schemas import IncomingMessage
from services import whatsapp_service
from services.claude_service import execute_approved_action, process_message
from services.tts_service import should_use_voice

router = APIRouter()
logger = logging.getLogger(__name__)

_YES = {"כן", "yes", "y", "אישור", "ok", "אוקיי"}
_NO = {"לא", "no", "n", "ביטול", "cancel"}
_VOICE_POSITIVE = {"יופי", "מושלם", "נהדר", "אהבתי", "קול", "תמשיך"}
_VOICE_NEGATIVE = {"תכתוב", "טקסט", "בטקסט", "מעצבן", "עצור", "הפסק"}


async def _log_voice_preference(context_type: str, used_voice: bool, feedback: str = "") -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(VoicePreference(
                context_type=context_type,
                used_voice=used_voice,
                user_feedback=feedback or None,
            ))
            await session.commit()
    except Exception as e:
        logger.error(f"[VOICE-PREF] Failed to log: {e}")


async def _handle_message(msg: IncomingMessage) -> None:
    logger.info(f"[WEBHOOK] message_type: {msg.message_type}")
    logger.info(f"[WEBHOOK] has audio: {bool(msg.media_data)}")
    logger.info(f"[WEBHOOK] message preview: {msg.message[:60]!r}")

    was_voice_input = msg.message_type == "audio"

    # --- Voice note: transcribe first ---
    text = msg.message.strip()
    if was_voice_input and msg.media_data:
        try:
            from services.voice_service import transcribe_voice
            text = await transcribe_voice(msg.media_data, msg.media_mime or "audio/ogg")
            if not text:
                await whatsapp_service.send_message("לא הצלחתי להבין, נסה שוב 🎤")
                return
            logger.info(f"[VOICE] Transcribed: {text[:80]}")
        except Exception as e:
            logger.error(f"[VOICE] Transcription failed: {e}")
            await whatsapp_service.send_message("❌ לא הצלחתי לתמלל את ההודעה הקולית.")
            return

    reply_chat_id = msg.group_id if msg.is_group else None
    lower = text.lower()

    # --- Voice feedback detection ---
    if not msg.is_group:
        feedback = ""
        if any(kw in lower for kw in _VOICE_POSITIVE):
            feedback = "positive"
        elif any(kw in lower for kw in _VOICE_NEGATIVE):
            feedback = "negative"
        if feedback:
            await _log_voice_preference("feedback", used_voice=feedback == "positive", feedback=feedback)

    # --- Approval flow (DM only) ---
    if not msg.is_group and lower in (_YES | _NO):
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
                    # Atomic update — only proceeds if still "pending" (prevents double-send)
                    upd = await session.execute(
                        update(PendingAction)
                        .where(PendingAction.id == pending.id)
                        .where(PendingAction.status == "pending")
                        .values(status="approved")
                        .returning(PendingAction.type, PendingAction.payload)
                    )
                    row = upd.first()
                    if not row:
                        await whatsapp_service.send_message("הפעולה כבר בוצעה.", chat_id=reply_chat_id)
                        return
                    action_type, payload = row.type, row.payload
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
        response_text, tool_used = await process_message(
            text,
            is_group=msg.is_group,
            group_sender=msg.group_sender,
        )

        # Decide voice vs text
        use_voice = should_use_voice(response_text, was_voice_input=was_voice_input)
        await _log_voice_preference("message", used_voice=use_voice)

        if use_voice and not msg.is_group:
            await whatsapp_service.send_voice_message(response_text, chat_id=reply_chat_id)
        else:
            await whatsapp_service.send_message(response_text, chat_id=reply_chat_id)

        # Background self-reflection (non-blocking)
        import asyncio
        from services.reflection_service import reflect_on_response
        asyncio.create_task(reflect_on_response(
            message_in=text,
            response_out=response_text,
            tool_used=tool_used,
            format_used="voice" if (use_voice and not msg.is_group) else "text",
        ))

        # Log group interactions
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
    if msg.sender.replace("+", "") != settings.owner_phone:
        logger.warning(f"[SECURITY] Ignored message from: {msg.sender}")
        return JSONResponse(status_code=200, content={"ok": True})

    background_tasks.add_task(_handle_message, msg)
    return {"ok": True}
