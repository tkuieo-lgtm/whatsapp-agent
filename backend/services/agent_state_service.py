"""
Dynamic agent state — fetched from DB on every conversation turn.
Keeps the system prompt current without redeploys.

Pattern: structural rules stay in code (versioned),
         runtime state comes from DB (dynamic).
"""
import logging
from typing import Optional

from sqlalchemy import func, select

from database import AsyncSessionLocal, GroupMember, Setting

logger = logging.getLogger(__name__)


async def get_system_notes() -> str:
    """
    Free-text notes the admin can update via API or frontend.
    Injected into every owner DM prompt so the agent learns about
    system changes without a redeploy.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == "system_notes")
            )
            s = result.scalar_one_or_none()
            return (s.value or "") if s else ""
    except Exception as e:
        logger.error(f"[AGENT_STATE] Failed to load system_notes: {e}")
        return ""


async def append_system_preference(preference: str) -> None:
    """
    Append a new owner preference to system_notes (called by the agent automatically).
    Creates the key if it doesn't exist; appends with a bullet point if it does.
    """
    try:
        from datetime import datetime, timezone
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Setting).where(Setting.key == "system_notes")
            )
            existing = result.scalar_one_or_none()
            current = (existing.value or "") if existing else ""

            new_line = f"• [{now}] {preference}"
            updated = f"{current}\n{new_line}".strip()

            stmt = (
                pg_insert(Setting)
                .values(key="system_notes", value=updated)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": updated},
                )
            )
            await session.execute(stmt)
            await session.commit()
        logger.info(f"[AGENT_STATE] Saved preference: {preference!r}")
    except Exception as e:
        logger.error(f"[AGENT_STATE] Failed to save preference: {e}")
        raise


async def get_group_member_summary() -> str:
    """
    Auto-generated summary of current group members injected into the
    owner DM prompt so the agent knows the current permission landscape.
    """
    try:
        async with AsyncSessionLocal() as session:
            total = await session.scalar(select(func.count(GroupMember.id))) or 0
            approved = await session.scalar(
                select(func.count(GroupMember.id))
                .where(GroupMember.status == "approved")
            ) or 0
            pending = await session.scalar(
                select(func.count(GroupMember.id))
                .where(GroupMember.status == "pending_approval")
            ) or 0

            if total == 0:
                return ""

            lines = [f"מצב חברי קבוצות נוכחי: {total} חברים — {approved} מאושרים, {pending} ממתינים לאישור."]

            # List pending approvals so owner can act
            if pending > 0:
                result = await session.execute(
                    select(GroupMember)
                    .where(GroupMember.status == "pending_approval")
                    .limit(5)
                )
                pending_members = result.scalars().all()
                lines.append("ממתינים לאישורך:")
                for m in pending_members:
                    lines.append(f"  - {m.email or 'ללא מייל'} ({m.phone}) בקבוצה {m.group_id[:20]}…")

            return "\n".join(lines)
    except Exception as e:
        logger.error(f"[AGENT_STATE] Failed to load group summary: {e}")
        return ""
