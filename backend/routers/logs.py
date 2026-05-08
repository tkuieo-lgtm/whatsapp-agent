from datetime import datetime, timezone

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from database import ActionLog, AsyncSessionLocal

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(
    limit: int = Query(50, le=200),
    action_type: str = Query(None),
):
    async with AsyncSessionLocal() as session:
        query = (
            select(ActionLog)
            .order_by(ActionLog.created_at.desc())
            .limit(limit)
        )
        if action_type:
            query = query.where(ActionLog.action_type == action_type)
        result = await session.execute(query)
        return [r.to_dict() for r in result.scalars().all()]


@router.get("/stats")
async def get_stats():
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    async with AsyncSessionLocal() as session:
        total = await session.scalar(
            select(func.count(ActionLog.id)).where(ActionLog.created_at >= today)
        ) or 0
        emails_handled = await session.scalar(
            select(func.count(ActionLog.id))
            .where(ActionLog.action_type == "email_rule_applied")
            .where(ActionLog.created_at >= today)
        ) or 0
    return {"total_actions_today": total, "emails_auto_handled_today": emails_handled}
