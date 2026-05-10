"""
Prompt-injection detection and group security enforcement.
"""
import logging
import re
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known injection patterns (case-insensitive)
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    # English
    r"ignore\s+(all\s+)?previous\s+instructions?",
    r"ignore\s+your\s+(instructions?|rules?|guidelines?)",
    r"forget\s+(all\s+)?(previous\s+)?(instructions?|rules?|guidelines?|your\s+role)",
    r"you\s+(are\s+now|have\s+no\s+(limits|restrictions))",
    r"imagine\s+you\s+are",
    r"pretend\s+(to\s+be|you\s+are)",
    r"act\s+as\s+(if\s+)?you\s+are",
    r"developer\s+mode",
    r"\bDAN\b",
    r"jailbreak",
    r"do\s+anything\s+now",
    r"no\s+restrictions?",
    r"bypass\s+(your\s+)?(rules?|guidelines?|filters?)",
    # Hebrew
    r"דמיין\s+ש(אתה|אות)",
    r"תדמיין\s+ש(אתה|אות)",
    r"תהיה\s+בוט\s+(אחר|חדש)",
    r"אני\s+(המפעיל|הבעלים|הבעל|אלון|ה-?owner)",
    r"שכח\s+את\s+(כל\s+)?(ה?הגבלות|ה?חוקים|ה?הוראות)",
    r"אין\s+לך\s+(הגבלות|כללים|חוקים)",
    r"אתה\s+(עכשיו\s+)?(בוט\s+אחר|חופשי|ללא\s+הגבלות)",
    r"תעביר\s+את\s+ההודעה\s+הבאה",
    r"הוראה\s+סודית",
]

_INJECTION_RE = [re.compile(p, re.IGNORECASE) for p in _INJECTION_PATTERNS]

# Zero-width and invisible characters
_INVISIBLE_CHARS = {0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0xFEFF, 0x2060, 0x2061}

# Base64-ish: long runs of base64 chars with no spaces
_BASE64_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def detect_injection(text: str) -> Optional[str]:
    """
    Returns a short description of the injection type if detected, else None.
    """
    # Pattern match
    for pattern in _INJECTION_RE:
        if pattern.search(text):
            return f"pattern: {pattern.pattern[:40]}"

    # Invisible / zero-width characters
    for ch in text:
        if ord(ch) in _INVISIBLE_CHARS:
            return f"zero-width char U+{ord(ch):04X}"

    # Suspicious base64 blob
    if _BASE64_RE.search(text):
        return "base64-encoded payload"

    return None


async def handle_injection_attempt(
    text: str,
    sender_phone: str,
    group_id: str,
    reason: str,
) -> str:
    """Log, alert OWNER, return a neutral refusal."""
    logger.warning(
        f"[SECURITY] Injection attempt from {sender_phone!r} in {group_id!r}: "
        f"reason={reason!r} text={text[:80]!r}"
    )

    # Store in action_log for audit trail
    try:
        from database import ActionLog, AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            session.add(ActionLog(
                action_type="injection_attempt",
                details={"phone": sender_phone, "group": group_id, "reason": reason, "text": text[:200]},
                status="blocked",
            ))
            await session.commit()
    except Exception as e:
        logger.error(f"[SECURITY] Failed to log injection attempt: {e}")

    # Alert OWNER
    try:
        from services import whatsapp_service
        alert = (
            f"⚠️ *ניסיון injection זוהה*\n"
            f"חבר: {sender_phone}\nקבוצה: {group_id}\n"
            f"סיבה: {reason}\n"
            f"טקסט: {text[:100]!r}"
        )
        await whatsapp_service.send_message(alert)
    except Exception as e:
        logger.error(f"[SECURITY] Failed to send injection alert: {e}")

    return "אין לי אפשרות לשנות הרשאות בשיחה."


# ---------------------------------------------------------------------------
# Group member helpers
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def extract_email(text: str) -> Optional[str]:
    """Return the first valid email address found in text, or None."""
    for token in text.split():
        token = token.strip(".,;!?\"'()")
        if _EMAIL_RE.match(token):
            return token.lower()
    return None


def is_owner(phone: str) -> bool:
    owner = settings.owner_phone.replace("+", "").replace("-", "")
    candidate = phone.replace("+", "").replace("-", "").replace(" ", "")
    return candidate[-9:] == owner[-9:]
