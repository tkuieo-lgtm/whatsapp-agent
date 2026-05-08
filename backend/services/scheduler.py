import logging
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from config import settings
from database import ActionLog, AsyncSessionLocal
from services import calendar_service, gmail_service, whatsapp_service
from services.rules_engine import run_email_rules
from services.reminder_service import check_and_send_reminders

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def _morning_summary() -> None:
    try:
        events = await calendar_service.get_todays_events()
        emails = await gmail_service.get_emails_awaiting_reply(hours_threshold=0)

        lines = ["🌅 *בוקר טוב! הנה הסיכום שלך להיום:*\n"]
        lines.append("📅 *יומן:*")
        if events:
            for ev in events:
                lines.append(f"- {ev['time']} — {ev['title']}")
        else:
            lines.append("- אין אירועים להיום")

        lines.append("")
        important = emails[:5]
        if important:
            lines.append(f"📧 *מיילים שדורשים תשובה ({len(important)}):*")
            for em in important:
                lines.append(f"- מ: {em['from']} — \"{em['subject']}\"")
        else:
            lines.append("📧 *מיילים:* אין מיילים דחופים")

        lines.append("\nיום טוב! 😊")
        await whatsapp_service.send_message("\n".join(lines))
        logger.info("[SCHEDULER] Morning summary sent.")
    except Exception as e:
        logger.error(f"[SCHEDULER] Morning summary failed: {e}")
        await whatsapp_service.send_message("❌ הייתה בעיה בשליחת הסיכום הבוקר.")


async def _weekly_summary() -> None:
    try:
        one_week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        async with AsyncSessionLocal() as session:
            claude_calls = await session.scalar(
                select(func.count(ActionLog.id))
                .where(ActionLog.action_type == "claude_call")
                .where(ActionLog.created_at >= one_week_ago)
            ) or 0
            rules_applied = await session.scalar(
                select(func.count(ActionLog.id))
                .where(ActionLog.action_type == "email_rule_applied")
                .where(ActionLog.created_at >= one_week_ago)
            ) or 0

        pending_emails = await gmail_service.get_emails_awaiting_reply(hours_threshold=0)

        lines = [
            "📊 *סיכום השבוע:*\n",
            "✅ *מה הושלם:*",
            f"- {claude_calls} אינטראקציות עם הסוכן",
            f"- {rules_applied} מיילים טופלו אוטומטית",
            "",
            "⏳ *עדיין פתוח:*",
        ]
        if pending_emails:
            lines.append(f"- {len(pending_emails)} מיילים ממתינים לתשובה")
        else:
            lines.append("- אין מיילים פתוחים")

        lines.append("\nשבת שלום! 🕍")
        await whatsapp_service.send_message("\n".join(lines))
        logger.info("[SCHEDULER] Weekly summary sent.")
    except Exception as e:
        logger.error(f"[SCHEDULER] Weekly summary failed: {e}")


async def _check_email_reminders() -> None:
    try:
        emails = await gmail_service.get_emails_awaiting_reply(
            hours_threshold=settings.reminder_threshold_hours
        )
        for em in emails:
            msg = (
                f"📬 *תזכורת:* יש לך מייל מ-{em['from']} שממתין לתשובה\n"
                f"נושא: \"{em['subject']}\""
            )
            await whatsapp_service.send_message(msg)
    except Exception as e:
        logger.error(f"[SCHEDULER] Email reminder check failed: {e}")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_scheduler() -> None:
    tz = pytz.timezone(settings.timezone)

    scheduler.add_job(
        _morning_summary,
        CronTrigger(hour=settings.morning_summary_hour, minute=0, timezone=tz),
        id="morning_summary",
    )
    scheduler.add_job(
        _weekly_summary,
        CronTrigger(
            day_of_week=settings.weekly_summary_day,
            hour=settings.weekly_summary_hour,
            minute=0,
            timezone=tz,
        ),
        id="weekly_summary",
    )
    scheduler.add_job(
        _check_email_reminders,
        CronTrigger(hour=f"*/{settings.reminder_check_hours}", minute=0, timezone=tz),
        id="email_reminders",
    )
    scheduler.add_job(
        run_email_rules,
        CronTrigger(minute="*/15", timezone=tz),
        id="email_rules",
    )
    scheduler.add_job(
        check_and_send_reminders,
        CronTrigger(minute="*", timezone=tz),   # every minute
        id="reminders",
    )
    scheduler.start()
    logger.info("[SCHEDULER] All jobs scheduled.")
