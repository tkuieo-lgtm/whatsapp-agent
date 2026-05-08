import logging
from datetime import datetime, timezone
from typing import Dict, List

from sqlalchemy import or_, select

from database import AsyncSessionLocal, Memory

logger = logging.getLogger(__name__)

CATEGORIES = {"people", "projects", "preferences", "episodic"}


async def remember(content: str, category: str) -> str:
    """Persist a fact to long-term memory."""
    if category not in CATEGORIES:
        category = "episodic"
    async with AsyncSessionLocal() as session:
        session.add(Memory(category=category, content=content))
        await session.commit()
    logger.info(f"[MEMORY] Saved [{category}]: {content[:80]}")
    return f"✅ נשמר בזיכרון [{category}]: {content}"


async def recall(query: str, limit: int = 10) -> List[Dict]:
    """Find memories relevant to the query via keyword search."""
    keywords = [kw for kw in query.split() if len(kw) > 2][:6]
    if not keywords:
        return []

    async with AsyncSessionLocal() as session:
        conditions = [Memory.content.ilike(f"%{kw}%") for kw in keywords]
        result = await session.execute(
            select(Memory)
            .where(or_(*conditions))
            .order_by(Memory.last_referenced.desc())
            .limit(limit)
        )
        memories = result.scalars().all()

        now = datetime.now(timezone.utc)
        for mem in memories:
            mem.last_referenced = now
        if memories:
            await session.commit()

        return [{"id": m.id, "category": m.category, "content": m.content} for m in memories]


async def load_context_for_message(message: str) -> str:
    """Return a formatted memory block to inject into the system prompt."""
    memories = await recall(message, limit=8)
    if not memories:
        return ""
    lines = ["📌 זיכרונות רלוונטיים:"]
    for m in memories:
        lines.append(f"  [{m['category']}] {m['content']}")
    return "\n".join(lines)
