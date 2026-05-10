import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import ActionLog, AsyncSessionLocal, GroupInteraction, GroupMember, PendingAction, VoicePreference
from models.schemas import IncomingMessage
from services import whatsapp_service
from services.claude_service import check_and_handle_approval, cleanup_stale_pending, execute_approved_action, process_message
from services.security_service import detect_injection, extract_email, handle_injection_attempt, is_owner
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
    sender_phone = msg.group_sender or msg.sender

    # =======================================================================
    # GROUP message path — security + permissions
    # =======================================================================
    if msg.is_group:
        group_id = msg.group_id or ""

        # 1. Injection detection (runs for everyone, including OWNER)
        injection_reason = detect_injection(text)
        if injection_reason:
            refusal = await handle_injection_attempt(text, sender_phone, group_id, injection_reason)
            await whatsapp_service.send_message(refusal, chat_id=reply_chat_id)
            return

        # 2. Load group member record
        group_member = None
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupMember)
                .where(GroupMember.phone == sender_phone)
                .where(GroupMember.group_id == group_id)
            )
            gm = result.scalar_one_or_none()
            if gm:
                group_member = {
                    "name": gm.name, "email": gm.email,
                    "status": gm.status, "phone": sender_phone,
                    "allowed_calendar_ids": gm.allowed_calendar_ids or [],
                }

        # 3. New group member — start registration flow
        if not group_member and not is_owner(sender_phone):
            # First time this person messages — create record + send welcome
            async with AsyncSessionLocal() as session:
                stmt = (
                    pg_insert(GroupMember)
                    .values(phone=sender_phone, group_id=group_id, status="awaiting_email")
                    .on_conflict_do_nothing()
                )
                await session.execute(stmt)
                await session.commit()
            welcome = (
                f"שלום! אני {settings.bot_name}, עוזר אישי של הבעלים 👋\n"
                f"כדי שאוכל לעזור לך — שלח לי את כתובת המייל שלך\n"
                f"(משמש לתיאום פגישות ולגישה למידע משותף)"
            )
            await whatsapp_service.send_message(welcome, chat_id=reply_chat_id)
            return

        # 4. Member awaiting email — handle email submission
        if group_member and group_member["status"] == "awaiting_email":
            email = extract_email(text)
            if email:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(GroupMember)
                        .where(GroupMember.phone == sender_phone)
                        .where(GroupMember.group_id == group_id)
                        .values(email=email, status="pending_approval")
                    )
                    await session.commit()
                # Notify OWNER
                await whatsapp_service.send_message(
                    f"📬 *חבר קבוצה חדש מבקש גישה*\n"
                    f"טלפון: {sender_phone}\nמייל: {email}\n"
                    f"קבוצה: {group_id}\n\n"
                    f"לאשר גישה? ענה *כן* או *לא*"
                )
                await whatsapp_service.send_message(
                    "תודה! נשלחה בקשת גישה לבעלים. נחכה לאישור.", chat_id=reply_chat_id
                )
                return
            else:
                await whatsapp_service.send_message(
                    "לא זיהיתי כתובת מייל. שלח בבקשה כתובת מייל תקינה.",
                    chat_id=reply_chat_id
                )
                return

        # 5. Pending approval — wait
        if group_member and group_member["status"] == "pending_approval":
            await whatsapp_service.send_message(
                "הבקשה שלך ממתינה לאישור הבעלים. בינתיים אין לי אפשרות לעזור.",
                chat_id=reply_chat_id
            )
            return

        # 6. Approved member or OWNER — process with permissions
        try:
            response_text, tool_used = await process_message(
                text,
                is_group=True,
                group_sender=sender_phone,
                group_member=group_member,
                channel="whatsapp",
            )
            await whatsapp_service.send_message(response_text, chat_id=reply_chat_id)
            async with AsyncSessionLocal() as session:
                session.add(GroupInteraction(
                    group_id=group_id, sender=sender_phone,
                    message=text, response=response_text,
                ))
                await session.commit()
        except Exception as e:
            logger.error(f"[GROUP] Error: {e}")
            await whatsapp_service.send_message("❌ אירעה שגיאה.", chat_id=reply_chat_id)
        return

    # =======================================================================
    # DIRECT MESSAGE path (unchanged)
    # =======================================================================

    # --- Voice feedback detection ---
    feedback = ""
    if any(kw in lower for kw in _VOICE_POSITIVE):
        feedback = "positive"
    elif any(kw in lower for kw in _VOICE_NEGATIVE):
        feedback = "negative"
    if feedback:
        await _log_voice_preference("feedback", used_voice=feedback == "positive", feedback=feedback)

    # --- Cleanup stale pending actions ---
    await cleanup_stale_pending("whatsapp")

    # --- Approval flow ---
    approval_result = await check_and_handle_approval(text, channel="whatsapp")
    if approval_result is not None:
        response_text, _ = approval_result
        await whatsapp_service.send_message(response_text)
        return

    # --- Process via Claude ---
    try:
        response_text, tool_used = await process_message(
            text,
            is_group=False,
            group_sender=None,
            channel="whatsapp",
        )

        # Decide voice vs text
        use_voice = should_use_voice(response_text, was_voice_input=was_voice_input)
        logger.info(f"[VOICE] use_voice={use_voice} was_voice={was_voice_input} words={len(response_text.split())} is_group={msg.is_group}")
        await _log_voice_preference("message", used_voice=use_voice)

        if use_voice and not msg.is_group:
            logger.info(f"[TTS] About to send {len(response_text.split())} word response as voice note")
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

    except Exception as e:
        logger.error(f"[MESSAGES] Error: {e}")
        await whatsapp_service.send_message("❌ אירעה שגיאה. אנא נסה שוב.")


@router.post("/webhook/message")
async def receive_message(msg: IncomingMessage, background_tasks: BackgroundTasks):
    if msg.sender.replace("+", "") != settings.owner_phone:
        logger.warning(f"[SECURITY] Ignored message from: {msg.sender}")
        return JSONResponse(status_code=200, content={"ok": True})

    background_tasks.add_task(_handle_message, msg)
    return {"ok": True}
