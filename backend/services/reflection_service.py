import json
import logging
import re

import anthropic
from sqlalchemy import func, select

from config import settings
from database import AsyncSessionLocal, Reflection

logger = logging.getLogger(__name__)

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

_EVAL_PROMPT = """\
הערך את איכות התגובה שלך ב-JSON בלבד — ללא שום טקסט נוסף.

הודעת המשתמש: "{message_in}"
תגובתך: "{response_out}"
כלי ששימש: {tool_used}
פורמט ששלחת: {format_used}

ספקת JSON:
{{"score": <1-5>, "improvement": "<משפט קצר אחד בעברית>"}}

קנה מידה: 5=פתרתי בדיוק את הבעיה, 4=טוב, 3=בסדר, 2=חלקי, 1=נכשלתי"""


async def reflect_on_response(
    message_in: str,
    response_out: str,
    tool_used: str = "",
    format_used: str = "text",
) -> None:
    """Background task: Claude evaluates its own response and saves to DB."""
    try:
        prompt = _EVAL_PROMPT.format(
            message_in=message_in[:300],
            response_out=response_out[:400],
            tool_used=tool_used or "ללא",
            format_used=format_used,
        )
        resp = _client.messages.create(
            model=settings.claude_model,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text if resp.content else ""
        match = re.search(r"\{.*?\}", raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            score = max(1, min(5, int(data.get("score", 3))))
            note = str(data.get("improvement", ""))[:300]
        else:
            score, note = 3, ""

        async with AsyncSessionLocal() as session:
            session.add(Reflection(
                message_in=message_in[:500],
                response_out=response_out[:500],
                tool_used=tool_used[:200] if tool_used else "",
                format_used=format_used,
                reflection_score=score,
                improvement_note=note,
            ))
            await session.commit()

        logger.info(f"[REFLECT] {score}/5 — {note}")
        await _maybe_send_summary()

    except Exception as e:
        logger.error(f"[REFLECT] Error: {type(e).__name__}: {e}")


async def _maybe_send_summary() -> None:
    """Send a performance summary to WhatsApp every 20 reflections."""
    try:
        from services import whatsapp_service

        async with AsyncSessionLocal() as session:
            total = await session.scalar(select(func.count(Reflection.id))) or 0
            if total == 0 or total % 20 != 0:
                return

            result = await session.execute(
                select(Reflection).order_by(Reflection.created_at.desc()).limit(20)
            )
            last20 = result.scalars().all()

        avg = sum(r.reflection_score for r in last20 if r.reflection_score) / len(last20)
        best = max(last20, key=lambda r: r.reflection_score or 0)
        worst = min(last20, key=lambda r: r.reflection_score or 5)

        msg = (
            f"📊 *סיכום ביצועים — {total} הודעות:*\n\n"
            f"ציון ממוצע: {avg:.1f}/5\n"
            f"עבד טוב: {best.improvement_note or 'תגובה מדויקת'}\n"
            f"לשיפור: {worst.improvement_note or 'ללא הערות'}"
        )
        await whatsapp_service.send_message(msg)
        logger.info(f"[REFLECT] Performance summary sent (total={total})")

    except Exception as e:
        logger.error(f"[REFLECT] Summary error: {e}")
