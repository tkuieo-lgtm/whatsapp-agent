import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)

_app = None   # global Telegram Application

_VOICE_NEGATIVE = {"תכתוב", "בטקסט", "טקסט", "write"}
_VOICE_POSITIVE = {"תקריא", "תגיד", "הקרא", "speak"}


async def _tts_send(update, response_text: str, force_text: bool = False, force_voice: bool = False, was_voice_input: bool = False) -> str:
    """
    Send response as voice or text based on explicit preference and auto-detection.
    Returns "voice" or "text".
    """
    from services.tts_service import should_use_voice, text_to_speech

    if force_text:
        use_voice = False
    elif force_voice:
        use_voice = True
    else:
        use_voice = should_use_voice(response_text, was_voice_input=was_voice_input)

    logger.info(f"[TELEGRAM] force_text={force_text} force_voice={force_voice} was_voice={was_voice_input} use_voice={use_voice}")

    if use_voice:
        try:
            audio_bytes = await text_to_speech(response_text)
            logger.info(f"[TELEGRAM] Sending voice note: {len(audio_bytes)} bytes")
            await update.message.reply_voice(voice=audio_bytes)
            return "voice"
        except Exception as e:
            logger.error(f"[TELEGRAM] TTS failed: {type(e).__name__}: {e} — falling back to text")

    await update.message.reply_text(response_text)
    return "text"


async def start_telegram_bot() -> None:
    global _app
    if not settings.telegram_bot_token:
        logger.info("[TELEGRAM] No TELEGRAM_BOT_TOKEN — skipping")
        return
    if not settings.owner_telegram_id:
        logger.warning("[TELEGRAM] OWNER_TELEGRAM_ID not set — bot will reject all users")

    from telegram import Update
    from telegram.ext import Application, CommandHandler, MessageHandler, filters

    _app = Application.builder().token(settings.telegram_bot_token).build()

    def _is_owner(update: Update) -> bool:
        return str(update.effective_user.id) == settings.owner_telegram_id

    # ---------------------------------------------------------------------------
    # /start
    # ---------------------------------------------------------------------------
    async def cmd_start(update: Update, context):
        if not _is_owner(update):
            await update.message.reply_text("לא מורשה.")
            return
        await update.message.reply_text(f"שלום! אני {settings.bot_name} 👋 במה אוכל לעזור?")

    # ---------------------------------------------------------------------------
    # Group member helpers — uses Telegram user_id as "phone", chat_id as group_id
    # ---------------------------------------------------------------------------
    async def _get_tg_group_member(user_id: int, chat_id: int) -> dict | None:
        from sqlalchemy import select
        from database import AsyncSessionLocal, GroupMember
        phone_key = str(user_id)
        group_key = f"tg:{chat_id}"
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(GroupMember)
                .where(GroupMember.phone == phone_key)
                .where(GroupMember.group_id == group_key)
            )
            gm = result.scalar_one_or_none()
            if gm:
                return {
                    "name": gm.name or str(user_id),
                    "email": gm.email,
                    "status": gm.status,
                    "phone": phone_key,
                    "allowed_calendar_ids": gm.allowed_calendar_ids or [],
                }
        return None

    async def _create_tg_group_member(user_id: int, chat_id: int, name: str) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from database import AsyncSessionLocal, GroupMember
        async with AsyncSessionLocal() as session:
            stmt = (
                pg_insert(GroupMember)
                .values(
                    phone=str(user_id),
                    group_id=f"tg:{chat_id}",
                    name=name,
                    status="awaiting_email",
                )
                .on_conflict_do_nothing()
            )
            await session.execute(stmt)
            await session.commit()

    async def _update_tg_group_member_email(user_id: int, chat_id: int, email: str) -> None:
        from sqlalchemy import update as sa_update
        from database import AsyncSessionLocal, GroupMember
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_update(GroupMember)
                .where(GroupMember.phone == str(user_id))
                .where(GroupMember.group_id == f"tg:{chat_id}")
                .values(email=email, status="pending_approval")
            )
            await session.commit()

    # ---------------------------------------------------------------------------
    # Text messages (DM + groups)
    # ---------------------------------------------------------------------------
    async def handle_text(update: Update, context):
        chat = update.effective_chat
        user = update.effective_user
        text = update.message.text or ""

        # Detect @mention via message entities
        bot_username = context.bot.username or ""
        mention = any(
            e.type == "mention"
            and text[e.offset:e.offset + e.length].lstrip("@").lower() == bot_username.lower()
            for e in (update.message.entities or [])
        )

        is_group = chat.type not in ("private",)
        owner = _is_owner(update)
        sender_id = user.id if user else 0
        logger.info(f"[TELEGRAM] chat_type={chat.type} is_group={is_group} sender={sender_id} is_owner={owner} mention={mention}")

        # =======================================================================
        # GROUP path
        # =======================================================================
        if is_group:
            # Only process if @mentioned OR from owner
            if not mention and not owner:
                return

            if owner:
                # Owner in group: full DM-like access (strip @mention, process as owner)
                cleaned = text.replace(f"@{bot_username}", "").strip()
                if not cleaned:
                    return
                text = cleaned
                lower = text.lower()
                force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
                force_voice = any(kw in lower for kw in _VOICE_POSITIVE)
                logger.info(f"[TELEGRAM] Group owner message: {text[:60]}")

                from services.claude_service import process_message, update_last_response_format
                response, _ = await process_message(text, channel="telegram")
                fmt = await _tts_send(update, response, force_text=force_text, force_voice=force_voice)
                await update_last_response_format("telegram", fmt)
                return

            # Non-owner @mentioned the bot
            cleaned = text.replace(f"@{bot_username}", "").strip()
            group_member = await _get_tg_group_member(sender_id, chat.id)
            sender_name = user.full_name or str(sender_id)

            # New member
            if not group_member:
                logger.info(f"[GROUP] New member detected: {sender_id} ({sender_name}) in tg:{chat.id}")
                await _create_tg_group_member(sender_id, chat.id, sender_name)
                await update.message.reply_text(
                    f"שלום {sender_name}! אני {settings.bot_name}, עוזר אישי של הבעלים 👋\n"
                    f"כדי שאוכל לעזור — שלח לי את כתובת המייל שלך."
                )
                return

            # Awaiting email
            if group_member["status"] == "awaiting_email":
                from services.security_service import extract_email
                email = extract_email(cleaned)
                if email:
                    await _update_tg_group_member_email(sender_id, chat.id, email)
                    # Notify owner
                    from services.whatsapp_service import send_message as wa_send
                    try:
                        await wa_send(
                            f"📬 *חבר Telegram חדש מבקש גישה*\n"
                            f"שם: {sender_name}\nID: {sender_id}\nמייל: {email}\n"
                            f"קבוצה: tg:{chat.id}\n\nלאשר גישה? ענה *כן* או *לא*"
                        )
                    except Exception as e:
                        logger.warning(f"[TELEGRAM] Owner WA notify failed: {e}")
                    await update.message.reply_text("תודה! נשלחה בקשת גישה לבעלים. נחכה לאישור.")
                else:
                    await update.message.reply_text("לא זיהיתי כתובת מייל. שלח בבקשה כתובת מייל תקינה.")
                return

            # Pending approval
            if group_member["status"] == "pending_approval":
                await update.message.reply_text("הבקשה שלך ממתינה לאישור הבעלים. אנא המתן.")
                return

            # Approved — process with group permissions
            if group_member["status"] == "approved":
                from services.claude_service import process_message, update_last_response_format
                response, _ = await process_message(
                    cleaned,
                    is_group=True,
                    group_sender=str(sender_id),
                    group_member=group_member,
                    channel="telegram",
                )
                await update.message.reply_text(response)
                await update_last_response_format("telegram", "text")
            return

        # =======================================================================
        # DM path — must be owner
        # =======================================================================
        if not owner:
            return

        lower = text.lower()
        force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
        force_voice = any(kw in lower for kw in _VOICE_POSITIVE)
        logger.info(f"[TELEGRAM] DM text: {text[:60]}")

        from services.claude_service import process_message, update_last_response_format
        response, _ = await process_message(text, channel="telegram")
        fmt = await _tts_send(update, response, force_text=force_text, force_voice=force_voice)
        await update_last_response_format("telegram", fmt)

    # ---------------------------------------------------------------------------
    # Voice notes (incoming) — owner only
    # ---------------------------------------------------------------------------
    async def handle_voice(update: Update, context):
        if not _is_owner(update):
            return
        logger.info("[TELEGRAM] Received voice note — downloading…")
        tmp_path = None
        try:
            voice_file = await update.message.voice.get_file()
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".ogg")
            os.close(tmp_fd)
            await voice_file.download_to_drive(tmp_path)

            with open(tmp_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()

            logger.info(f"[TELEGRAM] Downloaded {os.path.getsize(tmp_path)} bytes, transcribing…")

            from services.voice_service import transcribe_voice
            text = await transcribe_voice(audio_b64, "audio/ogg")
            if not text:
                await update.message.reply_text("לא הצלחתי להבין, נסה שוב 🎤")
                return

            logger.info(f"[TELEGRAM] Transcribed: {text[:80]}")

            # Voice input: default voice out, but still respect "תכתוב" if spoken
            lower = text.lower()
            force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
            force_voice = any(kw in lower for kw in _VOICE_POSITIVE)

            from services.claude_service import process_message, update_last_response_format
            response, _ = await process_message(text, channel="telegram")
            fmt = await _tts_send(update, response, force_text=force_text, force_voice=force_voice, was_voice_input=True)
            await update_last_response_format("telegram", fmt)

        except Exception as e:
            logger.error(f"[TELEGRAM] Voice error: {type(e).__name__}: {e}")
            await update.message.reply_text("שגיאה בעיבוד ההודעה הקולית.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ---------------------------------------------------------------------------
    # Audio files — owner only
    # ---------------------------------------------------------------------------
    async def handle_audio(update: Update, context):
        if not _is_owner(update):
            return
        logger.info("[TELEGRAM] Received audio file — treating as voice")
        tmp_path = None
        try:
            audio_file = await update.message.audio.get_file()
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".mp3")
            os.close(tmp_fd)
            await audio_file.download_to_drive(tmp_path)

            with open(tmp_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()

            from services.voice_service import transcribe_voice
            text = await transcribe_voice(audio_b64, "audio/mpeg")
            if not text:
                await update.message.reply_text("לא הצלחתי להבין, נסה שוב 🎤")
                return

            lower = text.lower()
            force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
            force_voice = any(kw in lower for kw in _VOICE_POSITIVE)

            from services.claude_service import process_message, update_last_response_format
            response, _ = await process_message(text, channel="telegram")
            fmt = await _tts_send(update, response, force_text=force_text, force_voice=force_voice, was_voice_input=True)
            await update_last_response_format("telegram", fmt)

        except Exception as e:
            logger.error(f"[TELEGRAM] Audio error: {type(e).__name__}: {e}")
            await update.message.reply_text("שגיאה בעיבוד הקובץ הקולי.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    _app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    _app.add_handler(MessageHandler(filters.AUDIO, handle_audio))

    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)
    logger.info("[TELEGRAM] Bot started polling (voice I/O + group support enabled)")


async def stop_telegram_bot() -> None:
    global _app
    if not _app:
        return
    try:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        logger.info("[TELEGRAM] Bot stopped")
    except Exception as e:
        logger.error(f"[TELEGRAM] Stop error: {e}")
