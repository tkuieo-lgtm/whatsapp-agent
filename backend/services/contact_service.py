import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import AsyncSessionLocal, Contact, WAGroup

logger = logging.getLogger(__name__)


async def save_contact(phone: str, name: str, email: Optional[str] = None, notes: Optional[str] = None) -> str:
    phone = phone.replace("+", "").replace(" ", "")
    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(Contact)
            .values(phone=phone, name=name, email=email, notes=notes)
            .on_conflict_do_update(
                index_elements=["phone"],
                set_={"name": name, "email": email or Contact.email, "notes": notes or Contact.notes},
            )
        )
        await session.execute(stmt)
        await session.commit()
    logger.info(f"[CONTACT] Saved: {name} ({phone}) email={email}")
    return f"✅ איש קשר נשמר: {name} ({phone})" + (f" מייל: {email}" if email else "")


async def get_contact(query: str) -> Optional[dict]:
    """Search by phone (last 9 digits) or name (partial match)."""
    async with AsyncSessionLocal() as session:
        # Try phone match (last 9 digits)
        q = query.replace("+", "").replace(" ", "")
        result = await session.execute(
            select(Contact).where(Contact.phone.endswith(q[-9:]))
        )
        c = result.scalar_one_or_none()

        if not c:
            # Try name match
            result = await session.execute(
                select(Contact).where(Contact.name.ilike(f"%{query}%"))
            )
            c = result.scalars().first()

        if not c:
            return None
        return {"phone": c.phone, "name": c.name, "email": c.email, "notes": c.notes}


async def get_contact_email(name_or_phone: str) -> Optional[str]:
    c = await get_contact(name_or_phone)
    return c.get("email") if c else None


async def save_group(group_id: str, name: str, channel: str = "whatsapp", member_count: Optional[int] = None) -> str:
    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(WAGroup)
            .values(group_id=group_id, name=name, channel=channel, member_count=member_count)
            .on_conflict_do_update(
                index_elements=["group_id"],
                set_={"name": name, "member_count": member_count},
            )
        )
        await session.execute(stmt)
        await session.commit()
    logger.info(f"[GROUP] Saved: {name} ({group_id})")
    return f"✅ קבוצה נשמרה: {name}"


async def get_group(group_id: str) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(WAGroup).where(WAGroup.group_id == group_id))
        g = result.scalar_one_or_none()
        if not g:
            return None
        return {"group_id": g.group_id, "name": g.name, "channel": g.channel, "member_count": g.member_count}
