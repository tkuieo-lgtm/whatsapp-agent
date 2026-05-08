from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import AsyncSessionLocal, Setting
from models.schemas import SettingUpdate

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
async def get_all_settings():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Setting))
        return {s.key: s.value for s in result.scalars().all()}


@router.get("/{key}")
async def get_setting(key: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Setting).where(Setting.key == key))
        setting = result.scalar_one_or_none()
        if not setting:
            raise HTTPException(status_code=404, detail="Setting not found")
        return {"key": setting.key, "value": setting.value}


@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate):
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(Setting)
            .values(key=key, value=body.value, updated_at=now)
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": body.value, "updated_at": now},
            )
        )
        await session.execute(stmt)
        await session.commit()
    return {"key": key, "value": body.value}
