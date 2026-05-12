import logging
from datetime import datetime, timedelta, timezone

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from config import settings
from database import ActionLog, AsyncSessionLocal, Reminder
from services import calendar_service, gmail_service, whatsapp_service  # gmail+wa used by morning/weekly summaries
from services.rules_engine import run_email_rules
from services.reminder_service import check_and_send_reminders

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=settings.timezone)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

async def _morning_summary() -> None:
    """Daily morning briefing at 07:30 Israel time — sent as voice note."""
    try:
        events = await calendar_service.get_todays_events()
        emails = await gmail_service.get_emails_awaiting_reply(hours_threshold=0)

        # Overdue reminders from yesterday
        yesterday_start = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        yesterday_end = yesterday_start + timedelta(days=1)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Reminder)
                .where(Reminder.sent.is_(True))
                .where(Reminder.remind_at >= yesterday_start)
                .where(Reminder.remind_at < yesterday_end)
            )
            overdue = result.scalars().all()

        # Load today-relevant memories
        from services.memory_service import load_context_for_message
        today_str = datetime.now(pytz.timezone(settings.timezone)).strftime("%Y-%m-%d")
        mem_context = await load_context_for_message(today_str)

        lines = [f"בוקר טוב! הנה הסיכום שלך להיום:\n"]

        lines.append("יומן:")
        if events:
            for ev in events:
                lines.append(f"{ev['time']} — {ev['title']}")
        else:
            lines.append("אין אירועים להיום")

        if overdue:
            lines.append(f"\nמאתמול — עדיין פתוח:")
            for rem in overdue:
                lines.append(f"• {rem.text}")

        if emails[:3]:
            lines.append(f"\nמיילים שדורשים תשובה ({len(emails)}):")
            for em in emails[:3]:
                lines.append(f"• {em['from']} — {em['subject']}")

        if mem_context:
            lines.append(f"\nתזכורת לעצמי: {mem_context[:200]}")

        lines.append("\nיום טוב!")
        full_text = "\n".join(lines)

        # Send as voice note
        sent = await whatsapp_service.send_voice_message(full_text, context_type="morning_briefing")
        if not sent:
            await whatsapp_service.send_message(f"🌅 *בוקר טוב!*\n\n{full_text}")
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
            f"- {claude_calls} אינטראקציות עם הסוכן",
            f"- {rules_applied} מיילים טופלו אוטומטית",
        ]
        if pending_emails:
            lines.append(f"- {len(pending_emails)} מיילים ממתינים לתשובה")
        lines.append("\nשבת שלום! 🕍")
        await whatsapp_service.send_message("\n".join(lines))
        logger.info("[SCHEDULER] Weekly summary sent.")
    except Exception as e:
        logger.error(f"[SCHEDULER] Weekly summary failed: {e}")


# _check_email_reminders and _check_proactive_alerts removed —
# unsolicited email notifications are not user-requested behavior.
# The agent responds to emails only when explicitly asked.


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

def setup_scheduler() -> None:
    tz = pytz.timezone(settings.timezone)

    scheduler.add_job(
        _morning_summary,
        CronTrigger(
            hour=settings.morning_summary_hour,
            minute=settings.morning_summary_minute,
            timezone=tz,
        ),
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
        run_email_rules,
        CronTrigger(minute="*/15", timezone=tz),
        id="email_rules",
    )
    scheduler.add_job(
        check_and_send_reminders,
        CronTrigger(minute="*", timezone=tz),
        id="reminders",
    )
    scheduler.start()
    logger.info("[SCHEDULER] All jobs scheduled.")
