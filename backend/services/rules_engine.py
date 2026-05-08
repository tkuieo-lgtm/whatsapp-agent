import logging
from typing import Dict

from sqlalchemy import select

from database import ActionLog, AsyncSessionLocal, EmailRule
from services import gmail_service

logger = logging.getLogger(__name__)


def _matches(email: Dict, conditions: Dict) -> bool:
    if fc := conditions.get("from_contains"):
        if fc.lower() not in email.get("from", "").lower():
            return False
    if sc := conditions.get("subject_contains"):
        if sc.lower() not in email.get("subject", "").lower():
            return False
    if tc := conditions.get("to_contains"):
        if tc.lower() not in email.get("to", "").lower():
            return False
    return True


async def _apply_actions(email: Dict, actions: Dict) -> None:
    if folder := actions.get("move_to_folder"):
        await gmail_service.move_to_folder(email["id"], folder)
    if actions.get("mark_as_read"):
        await gmail_service.mark_as_read(email["id"])


async def run_email_rules() -> None:
    """Apply active email rules to recent unread emails (runs every 15 minutes)."""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(EmailRule).where(EmailRule.is_active.is_(True))
            )
            rules = result.scalars().all()

        if not rules:
            return

        emails = await gmail_service.get_unread_emails(max_results=30, smart_filter=False)

        for email in emails:
            for rule in rules:
                if _matches(email, rule.conditions or {}):
                    await _apply_actions(email, rule.actions or {})
                    async with AsyncSessionLocal() as session:
                        session.add(ActionLog(
                            action_type="email_rule_applied",
                            details={"rule": rule.name, "email_id": email["id"]},
                            status="success",
                        ))
                        await session.commit()
                    logger.info(f"[RULES] Applied rule '{rule.name}' to email {email['id']}")
                    break  # first matching rule wins

    except Exception as e:
        logger.error(f"[RULES] Error running email rules: {e}")
