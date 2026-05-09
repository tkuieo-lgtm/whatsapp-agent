import base64
import logging
import os
import tempfile

from config import settings

logger = logging.getLogger(__name__)

_app = None   # global Telegram Application


async def start_telegram_bot() -> None:
    global _app
    if not settings.telegram_bot_token:
        logger.info("[TELEGRAM] No TELEGRAM_BOT_TOKEN — skipping")
        return
    if not settings.owner_telegram_id:
        logger.warning("[TELEGRAM] OWNER_TELEGRAM_ID not set — bot will reject all users")

    from telegram import Update
    from telegram.ext import Application, MessageHandler, CommandHandler, filters

    _app = Application.builder().token(settings.telegram_bot_token).build()

    # ---------------------------------------------------------------------------
    # Auth helper
    # ---------------------------------------------------------------------------
    def _is_owner(update: Update) -> bool:
        uid = str(update.effective_user.id)
        return uid == settings.owner_telegram_id

    # ---------------------------------------------------------------------------
    # Handlers
    # ---------------------------------------------------------------------------
    async def cmd_start(update: Update, context):
        if not _is_owner(update):
            await update.message.reply_text("לא מורשה.")
            return
        await update.message.reply_text(f"שלום! אני {settings.bot_name} 👋 במה אוכל לעזור?")

    async def handle_text(update: Update, context):
        if not _is_owner(update):
            return
        text = update.message.text or ""
        logger.info(f"[TELEGRAM] Received text: {text[:60]}")
        from services.claude_service import process_message
        response, _ = await process_message(text, channel="telegram")
        await update.message.reply_text(response)

    async def handle_voice(update: Update, context):
        if not _is_owner(update):
            return
        logger.info("[TELEGRAM] Received voice note")
        tmp_path = None
        try:
            voice_file = await update.message.voice.get_file()
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=".ogg")
            os.close(tmp_fd)
            await voice_file.download_to_drive(tmp_path)

            with open(tmp_path, "rb") as f:
                audio_b64 = base64.b64encode(f.read()).decode()

            from services.voice_service import transcribe_voice
            text = await transcribe_voice(audio_b64, "audio/ogg")
            if not text:
                await update.message.reply_text("לא הצלחתי להבין, נסה שוב 🎤")
                return

            await update.message.reply_text(f"🎤 {text}")

            from services.claude_service import process_message
            response, _ = await process_message(text, channel="telegram")
            await update.message.reply_text(response)

        except Exception as e:
            logger.error(f"[TELEGRAM] Voice error: {type(e).__name__}: {e}")
            await update.message.reply_text("שגיאה בעיבוד ההודעה הקולית.")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    _app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling(drop_pending_updates=True)
    logger.info("[TELEGRAM] Bot started polling")


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
