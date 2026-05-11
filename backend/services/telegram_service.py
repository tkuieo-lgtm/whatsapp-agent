import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)

_app = None   # global Telegram Application

_VOICE_NEGATIVE = {"תכתוב", "בטקסט", "טקסט", "write"}
_VOICE_POSITIVE = {"תקריא", "תגיד", "הקרא", "speak"}


async def _send_voice_response(update, response_text: str) -> None:
    """Generate TTS and send as a Telegram voice note, or fall back to text."""
    try:
        from services.tts_service import text_to_speech, should_use_voice
        if not should_use_voice(response_text, was_voice_input=True):
            await update.message.reply_text(response_text)
            return

        audio_bytes = await text_to_speech(response_text)
        logger.info(f"[TELEGRAM] Sending voice note: {len(audio_bytes)} bytes")
        await update.message.reply_voice(voice=audio_bytes)
    except Exception as e:
        logger.error(f"[TELEGRAM] TTS failed: {type(e).__name__}: {e} — falling back to text")
        await update.message.reply_text(response_text)


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
    # Text messages (DM + groups)
    # ---------------------------------------------------------------------------
    async def handle_text(update: Update, context):
        chat = update.effective_chat
        text = update.message.text or ""
        lower = text.lower()

        # Detect @mention via message entities
        bot_username = context.bot.username or ""
        mention = any(
            e.type == "mention"
            and text[e.offset:e.offset + e.length].lstrip("@").lower() == bot_username.lower()
            for e in (update.message.entities or [])
        )

        logger.info(f"[TELEGRAM] chat_type={chat.type} mention={mention} is_owner={_is_owner(update)}")

        # --- Group message ---
        if chat.type != "private":
            if not mention and not _is_owner(update):
                return  # ignore non-mention messages from non-owner in groups

            if not _is_owner(update):
                # Non-owner @mentioned the bot
                await update.message.reply_text(
                    "שלום! אני עוזר אישי פרטי ואינני זמין לחברים חיצוניים בקבוצות."
                )
                return

            # Owner in group — strip @mention and process
            cleaned = text.replace(f"@{bot_username}", "").strip()
            if not cleaned:
                return
            text = cleaned

        else:
            # Private DM — must be owner
            if not _is_owner(update):
                return

        logger.info(f"[TELEGRAM] Text: {text[:60]}")

        force_text  = any(kw in lower for kw in _VOICE_NEGATIVE)
        force_voice = any(kw in lower for kw in _VOICE_POSITIVE)

        from services.claude_service import process_message, update_last_response_format
        from services.tts_service import should_use_voice
        response, _ = await process_message(text, channel="telegram")

        auto_voice = should_use_voice(response, was_voice_input=False)
        if force_text:
            use_voice = False
        elif force_voice:
            use_voice = True
        else:
            use_voice = auto_voice

        fmt_used = "text"
        if use_voice:
            await _send_voice_response(update, response)
            fmt_used = "voice"
        else:
            await update.message.reply_text(response)

        await update_last_response_format("telegram", fmt_used)

    # ---------------------------------------------------------------------------
    # Voice notes (incoming)
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

            from services.claude_service import process_message, update_last_response_format
            response, _ = await process_message(text, channel="telegram")
            await _send_voice_response(update, response)
            await update_last_response_format("telegram", "voice")

        except Exception as e:
            logger.error(f"[TELEGRAM] Voice error: {type(e).__name__}: {e}")
            await update.message.reply_text("שגיאה בעיבוד ההודעה הקולית.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    # ---------------------------------------------------------------------------
    # Audio files (same as voice notes)
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

            from services.claude_service import process_message, update_last_response_format
            response, _ = await process_message(text, channel="telegram")
            await _send_voice_response(update, response)
            await update_last_response_format("telegram", "voice")

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
