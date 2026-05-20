import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from config import settings
from database import ActionLog, AsyncSessionLocal, GroupInteraction, GroupMember, PendingAction
from models.schemas import IncomingMessage
from services import whatsapp_service
from services.claude_service import check_and_handle_approval, cleanup_stale_pending, execute_approved_action, process_message, update_last_response_format
from services.security_service import detect_injection, extract_email, handle_injection_attempt, is_owner
from services.tts_service import should_use_voice

router = APIRouter()
logger = logging.getLogger(__name__)

_YES = {"כן", "yes", "y", "אישור", "ok", "אוקיי"}
_NO  = {"לא", "no", "n", "ביטול", "cancel"}
_VOICE_POSITIVE = {"יופי", "מושלם", "נהדר", "אהבתי", "קול", "תמשיך"}
_VOICE_NEGATIVE = {"תכתוב", "טקסט", "בטקסט", "מעצבן", "עצור", "הפסק"}


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
    lower         = text.lower()
    sender_phone  = msg.group_sender or msg.sender

    # =======================================================================
    # GROUP message path
    # =======================================================================
    if msg.is_group:
        group_id = msg.group_id or ""

        injection_reason = detect_injection(text)
        if injection_reason:
            refusal = await handle_injection_attempt(text, sender_phone, group_id, injection_reason)
            await whatsapp_service.send_message(refusal, chat_id=reply_chat_id)
            return

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

        if not group_member and not is_owner(sender_phone):
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

        # awaiting_intro: bot sent intro message, waiting for name + email
        if group_member and group_member["status"] == "awaiting_intro":
            name = msg.group_sender_name or ""
            email = extract_email(text)
            # Parse name from message if not from push name
            if not name:
                words = [w for w in text.split() if "@" not in w and len(w) > 1]
                if words:
                    name = " ".join(words[:3])
            update_vals: dict = {}
            if name:
                update_vals["name"] = name
            if email:
                update_vals["email"] = email
                update_vals["status"] = "pending_approval"
            elif name:
                update_vals["status"] = "awaiting_email"
            if update_vals:
                async with AsyncSessionLocal() as session:
                    await session.execute(
                        update(GroupMember)
                        .where(GroupMember.phone == sender_phone)
                        .where(GroupMember.group_id == group_id)
                        .values(**update_vals)
                    )
                    await session.commit()
            if email:
                await whatsapp_service.send_message(
                    f"📬 *חבר קבוצה חדש*\nשם: {name}\nטלפון: {sender_phone}\n"
                    f"מייל: {email}\nקבוצה: {group_id}\n\nלאשר גישה? ענה *כן* או *לא*"
                )
                await whatsapp_service.send_message(
                    f"תודה {name or ''}! הבקשה שלך ממתינה לאישור. נחכה לאישור.", chat_id=reply_chat_id
                )
            elif name:
                await whatsapp_service.send_message(
                    f"תודה {name}! מה כתובת המייל שלך?", chat_id=reply_chat_id
                )
            else:
                await whatsapp_service.send_message(
                    "מה שמך המלא וכתובת המייל שלך?", chat_id=reply_chat_id
                )
            return

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
                await whatsapp_service.send_message(
                    f"📬 *חבר קבוצה חדש מבקש גישה*\n"
                    f"טלפון: {sender_phone}\nמייל: {email}\n"
                    f"קבוצה: {group_id}\n\nלאשר גישה? ענה *כן* או *לא*"
                )
                await whatsapp_service.send_message(
                    "תודה! נשלחה בקשת גישה לבעלים. נחכה לאישור.", chat_id=reply_chat_id
                )
            else:
                await whatsapp_service.send_message(
                    "לא זיהיתי כתובת מייל. שלח בבקשה כתובת מייל תקינה.",
                    chat_id=reply_chat_id
                )
            return

        if group_member and group_member["status"] == "pending_approval":
            await whatsapp_service.send_message(
                "הבקשה שלך ממתינה לאישור הבעלים. בינתיים אין לי אפשרות לעזור.",
                chat_id=reply_chat_id
            )
            return

        # Auto-save group and sender contact if we have names
        if msg.group_name:
            from services.contact_service import save_group
            await save_group(group_id=group_id, name=msg.group_name, channel="whatsapp")
        if msg.group_sender_name and sender_phone and not is_owner(sender_phone):
            from services.contact_service import save_contact
            await save_contact(phone=sender_phone, name=msg.group_sender_name)

        try:
            response_text, _ = await process_message(
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
    # DIRECT MESSAGE path
    # =======================================================================

    force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
    force_voice = any(kw in lower for kw in _VOICE_POSITIVE)

    await cleanup_stale_pending("whatsapp")

    approval_result = await check_and_handle_approval(text, channel="whatsapp")
    if approval_result is not None:
        response_text, _ = approval_result
        await whatsapp_service.send_message(response_text)
        return

    try:
        response_text, tool_used = await process_message(
            text,
            is_group=False,
            group_sender=None,
            channel="whatsapp",
        )

        if force_text:
            use_voice = False
        elif force_voice:
            use_voice = True
        else:
            use_voice = should_use_voice(response_text, was_voice_input=was_voice_input)

        logger.info(
            f"[VOICE] use_voice={use_voice} was_voice={was_voice_input} "
            f"force_text={force_text} force_voice={force_voice} words={len(response_text.split())}"
        )

        if use_voice:
            await whatsapp_service.send_voice_message(response_text, chat_id=reply_chat_id)
        else:
            await whatsapp_service.send_message(response_text, chat_id=reply_chat_id)

        await update_last_response_format("whatsapp", "voice" if use_voice else "text")

    except Exception as e:
        logger.error(f"[MESSAGES] Error: {e}")
        await whatsapp_service.send_message("❌ אירעה שגיאה. אנא נסה שוב.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/webhook/message")
async def receive_message(msg: IncomingMessage, background_tasks: BackgroundTasks):
    if msg.sender.replace("+", "") != settings.owner_phone:
        logger.warning(f"[SECURITY] Ignored message from: {msg.sender}")
        return JSONResponse(status_code=200, content={"ok": True})
    background_tasks.add_task(_handle_message, msg)
    return {"ok": True}


@router.post("/webhook/group-joined")
async def group_joined(body: dict, background_tasks: BackgroundTasks):
    """Called by Baileys bridge when bot is added to a new group."""
    background_tasks.add_task(_handle_group_joined, body)
    return {"ok": True}


async def _handle_group_joined(body: dict) -> None:
    group_id   = body.get("group_id", "")
    group_name = body.get("group_name", group_id)
    members    = body.get("members", [])

    # Save group
    from services.contact_service import save_group
    await save_group(group_id=group_id, name=group_name, channel="whatsapp", member_count=len(members))
    logger.info(f"[GROUP-JOIN] {group_name} ({group_id}) — {len(members)} members")

    unknown = []
    for m in members:
        phone = m.get("phone", "")
        if not phone or is_owner(phone):
            continue
        # Check if already in DB
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupMember)
                .where(GroupMember.phone == phone)
                .where(GroupMember.group_id == group_id)
            )
            existing = result.scalar_one_or_none()
        if existing:
            continue   # already registered
        # Create with awaiting_intro
        async with AsyncSessionLocal() as session:
            stmt = (
                pg_insert(GroupMember)
                .values(phone=phone, group_id=group_id, status="awaiting_intro")
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)
            await session.commit()
        unknown.append(m.get("jid", f"{phone}@s.whatsapp.net"))

    if unknown:
        intro = (
            f"שלום לכולם! אני {settings.bot_name}, עוזר אישי של הבעלים 👋\n"
            f"כדי שאוכל לעזור לכם — אשמח לדעת את שמכם ומייל.\n"
            f"פשוט שלחו הודעה עם שמכם ומייל 😊"
        )
        await whatsapp_service.send_message(intro, chat_id=group_id)
        logger.info(f"[GROUP-JOIN] Sent intro to {len(unknown)} unknown members in {group_id}")


@router.post("/webhook/telegram")
async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives Telegram Bot API updates (webhook mode)."""
    from services.telegram_service import _app as tg_app
    if not tg_app:
        return JSONResponse(status_code=503, content={"error": "Telegram not configured"})
    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    from telegram import Update
    update = Update.de_json(data, tg_app.bot)
    background_tasks.add_task(tg_app.process_update, update)
    return {"ok": True}


@router.post("/webhook/alert")
async def receive_alert(body: dict):
    """Alert from Baileys bridge (e.g. max reconnects) — forwarded to owner via Telegram."""
    source  = body.get("source", "unknown")
    message = body.get("message", "")
    logger.error(f"[ALERT] from={source}: {message}")
    try:
        from services.telegram_service import _app as tg_app
        if tg_app and settings.owner_telegram_id:
            await tg_app.bot.send_message(chat_id=settings.owner_telegram_id, text=message)
            logger.info("[ALERT] Forwarded to owner via Telegram")
    except Exception as e:
        logger.warning(f"[ALERT] Telegram notify failed: {e}")
    return {"ok": True}
