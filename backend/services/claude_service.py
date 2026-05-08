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
        "description": "צור אירוע ביומן — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "HH:MM"},
                "end_time": {"type": "string", "description": "HH:MM"},
                "description": {"type": "string"},
                "location": {"type": "string"},
            },
            "required": ["title", "date", "start_time", "end_time"],
        },
    },
    {
        "name": "delete_calendar_event",
        "description": "מחק אירוע מהיומן — דורש אישור",
        "input_schema": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
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
]

# Group chats — no personal data tools
_TOOLS_GROUP: List[Dict] = [
    t for t in _TOOLS_DM
    if t["name"] in {
        "get_todays_events", "get_tomorrows_events", "get_weeks_events",
        "web_search",
    }
]

# Actions requiring explicit user approval
_WRITE_TOOLS = {"send_email", "create_calendar_event", "delete_calendar_event", "create_email_rule"}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SYSTEM_DM = """\
אתה עוזר AI אישי בשם {bot_name} שפועל דרך WhatsApp.
יש לך גישה מלאה ל-Gmail וליומן Google Calendar של המשתמש.

יכולות:
• קריאת מיילים, שליחה, חיפוש וסיכום לפי נושא
• צפייה, יצירה ומחיקה של אירועי יומן
• קביעת תזכורות ("תזכיר לי ביום שישי ב-17:00 לקנות מתנה")
• חיפוש מידע באינטרנט
• יצירת חוקי אוטומציה למיילים

כללים:
1. פעולות שמשנות נתונים (שליחת מייל, יצירת אירוע) דורשות אישור מפורש.
2. ענה תמיד בעברית, בצורה תמציתית וברורה.
3. לתזכורות — המר "מחר", "ביום שישי", "בעוד שעה" לתאריך ISO מדויק לפי השעה הנוכחית.

תאריך ושעה נוכחיים (ישראל): {current_datetime}
"""

_SYSTEM_GROUP = """\
אתה עוזר AI בשם {bot_name} שפועל בקבוצת WhatsApp.
אתה מסייע לחברי הקבוצה בנושאים ציבוריים בלבד.

יכולות בקבוצה:
• חיפוש מידע באינטרנט
• צפייה ביומן הבעלים לצורך תיאום פגישות

⚠️ אסור: לחשוף מיילים, תוכן יומן אישי, או כל מידע פרטי של הבעלים.
   לצורך תיאום בלבד — ניתן להציג חלונות זמן פנויים ללא פרטי האירוע.

ענה בעברית, בצורה קצרה וידידותית.

תאריך ושעה נוכחיים (ישראל): {current_datetime}
"""

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _save_history(role: str, content: str) -> None:
    try:
        async with AsyncSessionLocal() as session:
            session.add(ConversationHistory(role=role, content=content))
            await session.commit()
    except Exception as e:
        logger.error(f"[CLAUDE] Failed to save history: {e}")


async def _get_history(limit: int = 10) -> List[Dict]:
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


async def _create_pending_action(action_type: str, payload: Dict) -> str:
    async with AsyncSessionLocal() as session:
        action = PendingAction(type=action_type, payload=payload)
        session.add(action)
        await session.commit()
        await session.refresh(action)
        return str(action.id)


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
            return (result.scalar() or 0) < settings.claude_rate_limit_per_hour
    except Exception:
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

async def _execute_tool(name: str, inp: Dict, is_group: bool = False) -> Tuple[str, bool]:
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
                msg += "\n"
            elif name == "delete_calendar_event":
                msg = f"🗑️ *מחיקת אירוע* (ID: {inp['event_id']})\n\n"
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
            )
            return f"✅ האירוע '{payload['title']}' נוצר"

        if action_type == "delete_calendar_event":
            await calendar_service.delete_event(payload["event_id"])
            return "✅ האירוע נמחק"

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
) -> str:
    if not await _check_rate_limit():
        return "⚠️ הגעתי למגבלת הקריאות לשעה. נסה שוב מאוחר יותר."

    await _log_action("claude_call", {"message": user_message[:100], "is_group": is_group}, "started")
    await _save_history("user", user_message)

    history = await _get_history(limit=10)
    messages = [{"role": h["role"], "content": h["content"]} for h in history]
    if not messages or messages[-1]["role"] != "user":
        messages.append({"role": "user", "content": user_message})

    tz = pytz.timezone(settings.timezone)
    current_dt = datetime.now(tz).strftime("%Y-%m-%d %H:%M (%A)")

    if is_group:
        system = _SYSTEM_GROUP.format(bot_name=settings.bot_name, current_datetime=current_dt)
        tools = _TOOLS_GROUP
    else:
        system = _SYSTEM_DM.format(bot_name=settings.bot_name, current_datetime=current_dt)
        tools = _TOOLS_DM

    try:
        for _ in range(5):
            response = _client.messages.create(
                model=settings.claude_model,
                max_tokens=2048,
                system=system,
                tools=tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                text = "".join(b.text for b in response.content if hasattr(b, "text"))
                await _save_history("assistant", text)
                return text

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
                    result, needs_approval = await _execute_tool(b.name, b.input, is_group)
                    if needs_approval:
                        await _create_pending_action(b.name, b.input)
                        await _save_history("assistant", result)
                        return result
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": b.id, "content": result}
                    )
                messages.append({"role": "user", "content": tool_results})
            else:
                break

    except Exception as e:
        logger.error(f"[CLAUDE] Error: {e}")
        return f"❌ שגיאה בעיבוד ההודעה: {e}"

    return "מצטער, לא הצלחתי לעבד את הבקשה."
