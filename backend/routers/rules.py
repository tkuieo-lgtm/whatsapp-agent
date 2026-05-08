from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from database import AsyncSessionLocal, EmailRule
from models.schemas import EmailRuleCreate, EmailRuleUpdate

router = APIRouter(prefix="/api/rules", tags=["rules"])


@router.get("")
async def list_rules():
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EmailRule).order_by(EmailRule.created_at.desc())
        )
        return [r.to_dict() for r in result.scalars().all()]


@router.post("", status_code=201)
async def create_rule(body: EmailRuleCreate):
    async with AsyncSessionLocal() as session:
        rule = EmailRule(
            name=body.name,
            conditions=body.conditions,
            actions=body.actions,
            is_active=body.is_active,
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule.to_dict()


@router.put("/{rule_id}")
async def update_rule(rule_id: str, body: EmailRuleUpdate):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EmailRule).where(EmailRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")

        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        for key, value in updates.items():
            setattr(rule, key, value)

        await session.commit()
        await session.refresh(rule)
        return rule.to_dict()


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(EmailRule).where(EmailRule.id == rule_id)
        )
        rule = result.scalar_one_or_none()
        if rule:
            await session.delete(rule)
            await session.commit()
