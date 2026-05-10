import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import anthropic
import pytz
from sqlalchemy import func, select

from config import settings
from database import (
    ActionLog, AsyncSessionLocal, ConversationHistory,
    EmailRule, PendingAction, Reminder, Setting,
)
from services import calendar_service, gmail_service

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

# ---------------------------------------------------------------------------
# Tool definitions — full set (DM / owner)
# ---------------------------------------------------------------------------

_TOOLS_DM: List[Dict] = [
    {
        "name": "get_unread_emails",
        "description": "קבל מיילים לא נקראים מ-Gmail עם סינון חכם",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_results": {"type": "integer", "default": 10},
                "smart_filter": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "send_email",
        "description": "שלח מייל — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "cc": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "search_and_summarize_emails",
        "description": "חפש מיילים לפי נושא/שולח/מילות מפתח וקבל סיכום",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "מילות חיפוש, שם שולח, נושא"},
                "since_days": {"type": "integer", "default": 7, "description": "כמה ימים אחורה"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_todays_events",
        "description": "קבל אירועי יומן להיום (כולל שדה date בשעון ישראל)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_tomorrows_events",
        "description": "קבל אירועי יומן למחר (כולל שדה date בשעון ישראל)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_weeks_events",
        "description": "קבל אירועי יומן ל-7 ימים קרובים (כולל שדה date בשעון ישראל)",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_calendar_event",
        "description": "צור אירוע ביומן — דורש אישור. בחר calendar_id מהרשימה שב-system prompt לפי הקשר",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "HH:MM"},
                "end_time": {"type": "string", "description": "HH:MM"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "calendar_id": {"type": "string", "description": "'primary' לברירת מחדל, או id ספציפי מהרשימה"},
            },
            "required": ["title", "date", "start_time", "end_time"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "מחק אירוע מהיומן — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "description": "מזהה היומן, ברירת מחדל: primary"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "update_calendar_event",
        "description": "ערוך פרטים של אירוע קיים ביומן — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string"},
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "HH:MM"},
                "end_time": {"type": "string", "description": "HH:MM"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["event_id"],
        },
    },
    {
        "name": "move_calendar_event",
        "description": "העבר אירוע מיומן אחד לאחר — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "source_calendar_id": {"type": "string", "description": "יומן מקור, ברירת מחדל: primary"},
                "destination_calendar_id": {"type": "string", "description": "יומן יעד"},
            },
            "required": ["event_id", "destination_calendar_id"],
        },
    },
    {
        "name": "create_email_rule",
        "description": "צור חוק אוטומטי לטיפול במיילים — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "from_contains": {"type": "string"},
                "subject_contains": {"type": "string"},
                "move_to_folder": {"type": "string"},
                "mark_as_read": {"type": "boolean"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_reminder",
        "description": "קבע תזכורת לשעה ספציפית",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "מה להזכיר"},
                "remind_at": {
                    "type": "string",
                    "description": "תאריך ושעה ISO 8601 עם offset ישראל, לדוגמה: 2026-05-10T17:00:00+03:00",
                },
            },
            "required": ["text", "remind_at"],
        },
    },
    {
        "name": "web_search",
        "description": "חפש מידע באינטרנט דרך Tavily",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "remember",
        "description": "שמור עובדה חשובה לזיכרון לטווח ארוך. קטגוריות: people, projects, preferences, episodic",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "העובדה לשמירה"},
                "category": {
                    "type": "string",
                    "enum": ["people", "projects", "preferences", "episodic"],
                },
            },
            "required": ["content", "category"],
        },
    },
    {
        "name": "recall",
        "description": "חפש בזיכרון לטווח ארוך לפי מילות מפתח",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "מה לחפש בזיכרון"},
            },
            "required": ["query"],
        },
    },
]

# Group chats — no personal data tools
_TOOLS_GROUP: List[Dict] = [
    t for t in _TOOLS_DM
    if t["name"] in {
        "get_todays_events", "get_tomorrows_events", "get_weeks_events",
        "web_search", "recall",
    }
]

# Actions requiring explicit user approval
_WRITE_TOOLS = {
    "send_email", "create_calendar_event", "delete_calendar_event",
    "update_calendar_event", "move_calendar_event", "create_email_rule",
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_DM = """\
אתה {bot_name} — עוזר אישי חכם שפועל ב-WhatsApp.

## מי אתה
חבר שמכיר אותך טוב. גברי, ישיר, חם ולא רשמי. מדבר עברית שוטפת.
לא פותח תשובות ב"בהחלט!", "כמובן!", "מצוין!" — אלה ביטויים בוטיים.
אם לא בטוח במה שביקשו — שואל שאלה קצרה לפני שעושה.

## מה אתה יכול לעשות

**יומן Google Calendar**
- לראות אירועים: היום, מחר, השבוע
- ליצור אירוע (עם אישור)
- למחוק אירוע (עם אישור)

**מיילים Gmail**
- לראות מיילים לא נקראים (ללא ניוזלטרים)
- לחפש מיילים לפי שולח / נושא / מילות מפתח
- לשלוח מייל (עם אישור)
- ליצור חוק אוטומטי לסיווג מיילים (עם אישור)

**תזכורות**
- לקבוע תזכורת בשפה חופשית — "מחר ב-17:00", "בעוד שעה", "ביום שישי"
- תזכורות יישלחו אוטומטית בזמן שנקבע
- לתזכורות: המר לתאריך ISO מדויק לפי השעה הנוכחית

**חיפוש אינטרנטי**
- חיפוש מהיר דרך Tavily, 5 תוצאות עם סיכום

**זיכרון לטווח ארוך**
- לשמור עובדות: שמות, פרויקטים, העדפות, אירועים
- לשלוף מידע ישן לפי מילות מפתח
- כשהמשתמש מזכיר שם / פרויקט / העדפה — שמור אוטומטית

## מה אתה לא יכול לעשות
- לשלוח הודעות WhatsApp ישירות לאנשים אחרים
- לגשת לקבצים, תמונות, או אחסון מקומי
- לבצע תשלומים או הזמנות
- לייעץ רפואית / משפטית / פיננסית
- לזכור שיחות ישנות מעל ~10 הודעות (אלא אם שמרת בזיכרון מפורש)

## כללי תגובה — קול לעומת טקסט

**ברירת מחדל: קול.** אם המשתמש לא ביקש טקסט מפורשות — שלח קול.

**השתמש בטקסט רק כאשר:**
- התשובה ארוכה מאוד (מעל 120 מילה)
- יש נתונים מסודרים שחובה לראות: לו"ז עם זמנים, רשימת מיילים, קישורים
- המשתמש ביקש מפורשות "תכתוב", "בטקסט"

**חשוב ביותר:** קול **או** טקסט — לעולם לא שניהם באותה תגובה.
ב-WhatsApp: voice note. בWeb: קובץ אודיו שניגן בדפדפן. בשניהם — לא טקסט נוסף.

## פעולות שדורשות אישור
שליחת מייל, יצירת/מחיקת אירוע, יצירת חוק מייל —
לפני כל אחת מאלה: תאר בקצרה מה אתה עומד לעשות ושאל "להמשיך?"

## היכולות שלי לפי ערוץ
- **WhatsApp**: קול דו-כיווני ✓ | טקסט ✓
- **Web**: יוצר קובץ אודיו לניגון בדפדפן ✓ | מקבל הקלטת מיקרופון ✓ | טקסט ✓
- **טלגרם**: קול דו-כיווני ✓ | טקסט ✓

**חשוב**: ב-Web אני *כן* יכול לייצר קול. כשמבקשים "תקריא" — אני שולח אודיו שנשמע בדפדפן.
לעולם לא אגיד "אין לי יכולת קול בWeb" — אגיד "הנה:" ואתן את התוכן.
אני אותו {bot_name} בכל ערוץ — אותו מוח, אותה זיכרון.

תאריך ושעה נוכחיים (ישראל): {current_datetime}
{calendar_list}
{memory_context}"""

_SYSTEM_GROUP = """\
אתה {bot_name} — עוזר חכם בקבוצת WhatsApp.
גברי, ישיר, חם. מגיב רק כשמזכירים @{bot_name}.

## מה אתה יכול בקבוצה
- לחפש מידע באינטרנט
- לראות לו"ז פנוי של הבעלים לצורך תיאום (בלי פרטי האירועים עצמם)
- לענות על שאלות כלליות

## מה אסור
לחשוף מיילים, פרטי אירועים, או כל מידע אישי של הבעלים.
לתיאום: "הבעלים פנוי ב-X" — ללא פרטים נוספים.

ענה בעברית, קצר וידידותי.
תאריך ושעה נוכחיים (ישראל): {current_datetime}
"""



# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _save_history(role: str, content: str, channel: str = "whatsapp") -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(ConversationHistory(role=role, content=content, channel=channel))
            await session.commit()
    except Exception as e:
        logger.error(f"[CLAUDE] Failed to save history: {e}")


async def _get_history(limit: int = 20) -> List[Dict]:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationHistory)
                .order_by(ConversationHistory.created_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
            return [{"role": r.role, "content": r.content} for r in reversed(rows)]
    except Exception as e:
        logger.error(f"[CLAUDE] Failed to load history: {e}")
        return []


async def _create_pending_action(action_type: str, payload: Dict, channel: str = "whatsapp") -> str:
    async with AsyncSessionLocal() as session:
        action = PendingAction(type=action_type, payload=payload, channel=channel)
        session.add(action)
        await session.commit()
        await session.refresh(action)
        return str(action.id)


async def cleanup_stale_pending(channel: str, max_age_minutes: int = 3) -> None:
    """Delete old pending actions for this channel — call before every non-approval message."""
    from sqlalchemy import delete as sa_delete
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_delete(PendingAction)
            .where(PendingAction.channel == channel)
            .where(PendingAction.status == "pending")
            .where(PendingAction.created_at < cutoff)
        )
        deleted = result.rowcount
        await session.commit()
    if deleted:
        logger.info(f"[PENDING] Cleaned up {deleted} stale pending action(s) for channel={channel}")


async def _log_action(action_type: str, details: Dict, status: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(ActionLog(action_type=action_type, details=details, status=status))
            await session.commit()
    except Exception as e:
        logger.error(f"[CLAUDE] Failed to log action: {e}")


# ---------------------------------------------------------------------------
# Rate limiting + auto-mode
# ---------------------------------------------------------------------------

async def _check_rate_limit() -> bool:
    try:
        one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(func.count(ActionLog.id))
                .where(ActionLog.action_type == "claude_call")
                .where(ActionLog.created_at >= one_hour_ago)
            )
            count = result.scalar() or 0
        limit = settings.claude_rate_limit_per_hour
        logger.info(f"[RATE] Claude calls last hour: {count}/{limit}")
        return count < limit
    except Exception as e:
        logger.error(f"[RATE] Rate limit check failed: {e}")
        return True


async def _is_auto_mode(action_type: str) -> bool:
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == f"auto_mode_{action_type}")
            )
            s = result.scalar_one_or_none()
            return bool(s.value) if s else False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool(name: str, inp: Dict, is_group: bool = False, channel: str = "whatsapp") -> Tuple[str, bool]:
    """Execute a tool. Returns (result_text, needs_approval)."""
    try:
        # --- Read tools ---
        if name == "get_unread_emails":
            emails = await gmail_service.get_unread_emails(
                max_results=inp.get("max_results", 10),
                smart_filter=inp.get("smart_filter", True),
            )
            if not emails:
                return "אין מיילים לא נקראים שדורשים תשובה.", False
            lines = [f"נמצאו {len(emails)} מיילים:\n"]
            for i, em in enumerate(emails, 1):
                lines.append(f"{i}. מ: {em['from']}\n   נושא: {em['subject']}\n   {em['snippet'][:100]}…\n")
            return "\n".join(lines), False

        if name == "search_and_summarize_emails":
            emails = await gmail_service.search_emails(
                query=inp["query"], since_days=inp.get("since_days", 7)
            )
            if not emails:
                return f"לא נמצאו מיילים עבור: {inp['query']}", False
            lines = [f"נמצאו {len(emails)} מיילים עבור \"{inp['query']}\":\n"]
            for em in emails:
                lines.append(f"• מ: {em['from']} | נושא: {em['subject']}\n  {em['snippet'][:150]}…\n")
            return "\n".join(lines), False

        if name == "get_todays_events":
            events = await calendar_service.get_todays_events()
            if not events:
                return "אין אירועים ביומן להיום.", False
            lines = ["📅 אירועים להיום:"]
            for ev in events:
                loc = f" ({ev['location']})" if ev.get("location") else ""
                lines.append(f"• {ev['date']} {ev['time']} — {ev['title']}{loc}")
            return "\n".join(lines), False

        if name == "get_tomorrows_events":
            events = await calendar_service.get_tomorrows_events()
            if not events:
                return "אין אירועים ביומן למחר.", False
            lines = ["📅 אירועים למחר:"]
            for ev in events:
                loc = f" ({ev['location']})" if ev.get("location") else ""
                lines.append(f"• {ev['date']} {ev['time']} — {ev['title']}{loc}")
            return "\n".join(lines), False

        if name == "get_weeks_events":
            events = await calendar_service.get_weeks_events()
            if not events:
                return "אין אירועים ביומן השבוע.", False
            lines = ["📅 אירועים השבוע:"]
            for ev in events:
                loc = f" ({ev['location']})" if ev.get("location") else ""
                lines.append(f"• {ev['date']} {ev['time']} — {ev['title']}{loc}")
            return "\n".join(lines), False

        if name == "web_search":
            from services.search_service import web_search
            return await web_search(inp["query"], inp.get("max_results", 5)), False

        if name == "remember":
            from services.memory_service import remember
            return await remember(inp["content"], inp.get("category", "episodic")), False

        if name == "recall":
            from services.memory_service import recall
            memories = await recall(inp["query"])
            if not memories:
                return "לא נמצאו זיכרונות רלוונטיים.", False
            lines = [f"📌 זיכרונות רלוונטיים ({len(memories)}):"]
            for m in memories:
                lines.append(f"[{m['category']}] {m['content']}")
            return "\n".join(lines), False

        if name == "set_reminder":
            remind_at = datetime.fromisoformat(inp["remind_at"])
            async with AsyncSessionLocal() as session:
                session.add(Reminder(text=inp["text"], remind_at=remind_at))
                await session.commit()
            formatted = remind_at.strftime("%d/%m/%Y %H:%M")
            return f"✅ תזכורת נקבעה: {inp['text']}\n🕐 {formatted}", False

        # --- Write tools (require approval unless auto-mode) ---
        if name in _WRITE_TOOLS:
            if await _is_auto_mode(name):
                return await execute_approved_action(name, inp), False

            if name == "send_email":
                msg = f"📧 *שליחת מייל*\n\n*ל:* {inp['to']}\n"
                if inp.get("cc"):
                    msg += f"*CC:* {inp['cc']}\n"
                msg += f"*נושא:* {inp['subject']}\n\n*תוכן:*\n{inp['body']}\n\n"
            elif name == "create_calendar_event":
                msg = (
                    f"📅 *יצירת אירוע*\n\n*כותרת:* {inp['title']}\n"
                    f"*תאריך:* {inp['date']}\n*שעות:* {inp['start_time']} — {inp['end_time']}\n"
                )
                if inp.get("location"):
                    msg += f"*מיקום:* {inp['location']}\n"
                if inp.get("description"):
                    msg += f"*תיאור:* {inp['description']}\n"
                cal_id = inp.get("calendar_id", "primary")
                if cal_id and cal_id != "primary":
                    msg += f"*יומן:* {cal_id}\n"
                msg += "\n"
            elif name == "delete_calendar_event":
                cal = inp.get("calendar_id", "primary")
                msg = f"🗑️ *מחיקת אירוע* (ID: {inp['event_id']})\nיומן: {cal}\n\n"
            elif name == "update_calendar_event":
                msg = f"✏️ *עריכת אירוע* (ID: {inp['event_id']})\n"
                if inp.get("title"):
                    msg += f"*כותרת חדשה:* {inp['title']}\n"
                if inp.get("date"):
                    msg += f"*תאריך:* {inp['date']}\n"
                if inp.get("start_time"):
                    msg += f"*שעות:* {inp['start_time']} — {inp.get('end_time','')}\n"
                if inp.get("location"):
                    msg += f"*מיקום:* {inp['location']}\n"
                msg += "\n"
            elif name == "move_calendar_event":
                src = inp.get("source_calendar_id", "primary")
                dst = inp["destination_calendar_id"]
                msg = (
                    f"🔀 *העברת אירוע*\n"
                    f"מ: {src} → ל: {dst}\n"
                    f"ID: {inp['event_id']}\n\n"
                )
            elif name == "create_email_rule":
                cond = {k: inp[k] for k in ("from_contains", "subject_contains") if inp.get(k)}
                act = {k: inp[k] for k in ("move_to_folder", "mark_as_read") if inp.get(k) is not None}
                msg = (
                    f"📋 *חוק מייל חדש*\n\n*שם:* {inp['name']}\n"
                    f"*תנאים:* {json.dumps(cond, ensure_ascii=False)}\n"
                    f"*פעולות:* {json.dumps(act, ensure_ascii=False)}\n\n"
                )
            else:
                msg = f"פעולה: {name}\n{json.dumps(inp, ensure_ascii=False)}\n\n"

            msg += "להמשיך? ענה *כן* או *לא*"
            return msg, True

        return f"כלי לא מוכר: {name}", False

    except ValueError as e:
        msg = str(e)
        if "credentials not configured" in msg.lower():
            return f"Gmail לא מחובר. כנס ל: {settings.backend_url}/auth/google", False
        return f"❌ {msg}", False
    except Exception as e:
        logger.error(f"[CLAUDE] Tool error ({name}): {e}")
        return f"❌ שגיאה בביצוע {name}: {e}", False


async def execute_approved_action(action_type: str, payload: Dict) -> str:
    try:
        if action_type == "send_email":
            await gmail_service.send_email(
                to=payload["to"], subject=payload["subject"],
                body=payload["body"], cc=payload.get("cc"),
            )
            return f"✅ המייל נשלח ל-{payload['to']}"

        if action_type == "create_calendar_event":
            await calendar_service.create_event(
                title=payload["title"], date=payload["date"],
                start_time=payload["start_time"], end_time=payload["end_time"],
                description=payload.get("description", ""), location=payload.get("location", ""),
                calendar_id=payload.get("calendar_id", "primary"),
            )
            return f"✅ האירוע '{payload['title']}' נוצר ביומן"

        if action_type == "delete_calendar_event":
            await calendar_service.delete_event(
                payload["event_id"],
                calendar_id=payload.get("calendar_id", "primary"),
            )
            return "✅ האירוע נמחק מהיומן"

        if action_type == "update_calendar_event":
            await calendar_service.update_event(
                event_id=payload["event_id"],
                calendar_id=payload.get("calendar_id", "primary"),
                title=payload.get("title"),
                date=payload.get("date"),
                start_time=payload.get("start_time"),
                end_time=payload.get("end_time"),
                description=payload.get("description"),
                location=payload.get("location"),
            )
            return f"✅ האירוע עודכן"

        if action_type == "move_calendar_event":
            await calendar_service.move_event(
                event_id=payload["event_id"],
                source_calendar_id=payload.get("source_calendar_id", "primary"),
                destination_calendar_id=payload["destination_calendar_id"],
            )
            return f"✅ האירוע הועבר ל-{payload['destination_calendar_id']}"

        if action_type == "create_email_rule":
            conditions = {k: payload[k] for k in ("from_contains", "subject_contains") if payload.get(k)}
            actions = {k: payload[k] for k in ("move_to_folder", "mark_as_read") if payload.get(k) is not None}
            async with AsyncSessionLocal() as session:
                session.add(EmailRule(name=payload["name"], conditions=conditions, actions=actions))
                await session.commit()
            return f"✅ החוק '{payload['name']}' נוצר"

        return f"❌ סוג פעולה לא מוכר: {action_type}"
    except Exception as e:
        logger.error(f"[CLAUDE] Approved action error ({action_type}): {e}")
        return f"❌ שגיאה בביצוע הפעולה: {e}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def process_message(
    user_message: str,
    is_group: bool = False,
    group_sender: Optional[str] = None,
    channel: str = "whatsapp",
) -> Tuple[str, str]:   # (response_text, comma-separated tools used)
    if not await _check_rate_limit():
        return f"⚠️ הגעתי למגבלת {settings.claude_rate_limit_per_hour} קריאות לשעה. נסה שוב עוד כמה דקות.", ""

    await _log_action("claude_call", {"message": user_message[:100], "is_group": is_group, "channel": channel}, "started")
    await _save_history("user", user_message, channel=channel)

    history = await _get_history(limit=10)
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": user_message})

    tz = pytz.timezone(settings.timezone)
    _hebrew_days = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]
    now_il = datetime.now(tz)
    current_dt = f"{now_il.strftime('%Y-%m-%d %H:%M')} (יום {_hebrew_days[now_il.weekday()]})"

    if is_group:
        system = _SYSTEM_GROUP.format(bot_name=settings.bot_name, current_datetime=current_dt)
        tools = _TOOLS_GROUP
    elif channel != "whatsapp":
        from services.memory_service import load_context_for_message
        from services.calendar_service import get_calendar_list_for_prompt
        memory_context = await load_context_for_message(user_message)
        calendar_list = await get_calendar_list_for_prompt()
        system = _SYSTEM_DM.format(
            bot_name=settings.bot_name,
            current_datetime=current_dt,
            calendar_list=calendar_list,
            memory_context=memory_context,
        ) + f"\nערוץ נוכחי: {channel}"
        tools = _TOOLS_DM
    else:
        # Load relevant long-term memories for context
        from services.memory_service import load_context_for_message
        from services.calendar_service import get_calendar_list_for_prompt
        memory_context = await load_context_for_message(user_message)
        calendar_list = await get_calendar_list_for_prompt()
        system = _SYSTEM_DM.format(
            bot_name=settings.bot_name,
            current_datetime=current_dt,
            calendar_list=calendar_list,
            memory_context=memory_context,
        )
        tools = _TOOLS_DM

    tools_used: List[str] = []

    try:
        for iteration in range(5):
            logger.info(f"[CLAUDE] API call iteration {iteration+1}, messages={len(messages)}")
            response = _client.messages.create(
                model=settings.claude_model,
                max_tokens=2048,
                system=system,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                text = "".join(b.text for b in response.content if hasattr(b, "text"))
                await _save_history("assistant", text, channel=channel)
                return text, ",".join(tools_used)

            if response.stop_reason == "tool_use":
                assistant_content = []
                for b in response.content:
                    if b.type == "text":
                        assistant_content.append({"type": "text", "text": b.text})
                    elif b.type == "tool_use":
                        assistant_content.append(
                            {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                        )
                messages.append({"role": "assistant", "content": assistant_content})

                tool_results = []
                for b in response.content:
                    if b.type != "tool_use":
                        continue
                    tools_used.append(b.name)
                    result, needs_approval = await _execute_tool(b.name, b.input, is_group, channel)
                    if needs_approval:
                        await _create_pending_action(b.name, b.input, channel=channel)
                        await _save_history("assistant", result, channel=channel)
                        return result, ",".join(tools_used)
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": b.id, "content": result}
                    )
                messages.append({"role": "user", "content": tool_results})
            else:
                break

    except Exception as e:
        logger.error(f"[CLAUDE] Error: {type(e).__name__}: {e}")
        return f"❌ שגיאה בעיבוד ההודעה: {type(e).__name__}", ""

    return "מצטער, לא הצלחתי לעבד את הבקשה.", ""


# ---------------------------------------------------------------------------
# Shared approval handler — used by WhatsApp, Web chat, and Telegram
# ---------------------------------------------------------------------------

_YES_WORDS = {"כן", "yes", "y", "אישור", "ok", "אוקיי"}
_NO_WORDS  = {"לא", "no", "n", "ביטול", "cancel"}


async def check_and_handle_approval(
    text: str,
    channel: str = "whatsapp",
) -> Optional[Tuple[str, str]]:
    """
    If `text` is an approval/rejection response AND there is a pending action for this channel,
    execute or cancel it and return (response_text, tool_used).
    Returns None if text is not an approval keyword or there is no matching pending action.
    """
    from sqlalchemy import delete as sa_delete, update as sa_update
    from database import ActionLog, AsyncSessionLocal, PendingAction

    lower = text.lower().strip()
    if lower not in (_YES_WORDS | _NO_WORDS):
        return None

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PendingAction)
            .where(PendingAction.status == "pending")
            .where(PendingAction.channel == channel)   # channel isolation
            .order_by(PendingAction.created_at.desc())
            .limit(1)
        )
        pending = result.scalar_one_or_none()

    if not pending:
        return None

    if datetime.now(timezone.utc) > pending.expires_at:
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(PendingAction).where(PendingAction.id == pending.id)
            )
            await session.commit()
        return "⏰ הפעולה פגה. אנא בקש שוב.", ""

    if lower in _YES_WORDS:
        # Atomic delete — only proceeds if still "pending", prevents double execution
        async with AsyncSessionLocal() as session:
            del_result = await session.execute(
                sa_delete(PendingAction)
                .where(PendingAction.id == pending.id)
                .where(PendingAction.status == "pending")
                .returning(PendingAction.type, PendingAction.payload)
            )
            row = del_result.first()
            if not row:
                await session.commit()
                return "הפעולה כבר בוצעה.", ""
            action_type, payload = row.type, row.payload
            await session.commit()

        response = await execute_approved_action(action_type, payload)

        async with AsyncSessionLocal() as session:
            session.add(ActionLog(action_type=action_type, details=payload, status="approved"))
            await session.commit()

        await _save_history("assistant", response, channel=channel)
        return response, action_type

    else:
        # Rejected — delete from DB immediately
        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_delete(PendingAction).where(PendingAction.id == pending.id)
            )
            await session.commit()
        response = "✅ הפעולה בוטלה. איך אוכל לעזור?"
        await _save_history("assistant", response, channel=channel)
        return response, ""
