import base64
import email as email_lib
import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from database import AsyncSessionLocal, Setting

logger = logging.getLogger(__name__)

NEWSLETTER_PATTERNS = [r"noreply@", r"no-reply@", r"donotreply@", r"notifications@"]
NEWSLETTER_KEYWORDS = ["unsubscribe", "newsletter", "mailing list", "opt-out"]


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

async def get_credentials() -> Optional[Credentials]:
    """Load Google credentials from DB. Auto-refreshes if expired or invalid."""
    import traceback
    from config import settings as _settings

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Setting).where(Setting.key == "google_tokens")
        )
        setting = result.scalar_one_or_none()

    token_in_db = setting is not None
    logger.info(f"[GMAIL] token in DB: {token_in_db}")

    if not token_in_db:
        from config import settings as _s
        logger.error(f"[GMAIL] Re-auth required → {_s.backend_url}/auth/google")
        return None

    td = setting.value

    # Restore expiry so creds.expired works correctly
    expiry = None
    if td.get("expiry"):
        try:
            expiry = datetime.fromisoformat(td["expiry"])
        except Exception:
            pass

    creds = Credentials(
        token=td.get("access_token"),
        refresh_token=td.get("refresh_token"),
        token_uri=td.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=td.get("client_id"),
        client_secret=td.get("client_secret"),
        scopes=td.get("scopes", []),
        expiry=expiry,
    )

    logger.info(
        f"[GMAIL] Creds loaded — valid={creds.valid} expired={creds.expired} "
        f"has_refresh={bool(creds.refresh_token)} scopes={creds.scopes}"
    )
    # Warn about missing write scopes (causes silent permission failures)
    _required = {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.events",
    }
    _actual = set(creds.scopes or [])
    _missing = _required - _actual
    if _missing:
        logger.error(f"[GMAIL] ⚠️  Missing scopes: {_missing}")
        logger.error(f"[GMAIL] Re-auth required at: {_settings.backend_url}/auth/google")

    if (creds.expired or not creds.valid) and creds.refresh_token:
        logger.info("[GMAIL] Refreshing token…")
        try:
            creds.refresh(Request())
            await _save_credentials(creds, td.get("client_id"), td.get("client_secret"))
            logger.info("[GMAIL] Token refreshed successfully")
        except Exception as e:
            logger.error(f"[GMAIL] Token refresh failed: {type(e).__name__}: {e}")
            logger.error(f"[GMAIL] Full error:\n{traceback.format_exc()}")
            return None

    return creds


async def _save_credentials(creds: Credentials, client_id: str, client_secret: str) -> None:
    token_data = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri or "https://oauth2.googleapis.com/token",
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": list(creds.scopes) if creds.scopes else [],
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }
    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(Setting)
            .values(key="google_tokens", value=token_data, updated_at=datetime.now(timezone.utc))
            .on_conflict_do_update(
                index_elements=["key"],
                set_={"value": token_data, "updated_at": datetime.now(timezone.utc)},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def save_credentials(creds: Credentials, client_id: str, client_secret: str) -> None:
    await _save_credentials(creds, client_id, client_secret)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_body(message: Dict) -> str:
    def _extract(part: Dict) -> str:
        mime = part.get("mimeType", "")
        if mime == "text/plain":
            data = part.get("body", {}).get("data", "")
            if data:
                return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="ignore")
        if mime.startswith("multipart/"):
            for sub in part.get("parts", []):
                text = _extract(sub)
                if text:
                    return text
        return ""
    return _extract(message.get("payload", {}))


def _is_newsletter(message: Dict) -> bool:
    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }
    from_addr = headers.get("from", "").lower()
    for pattern in NEWSLETTER_PATTERNS:
        if re.search(pattern, from_addr):
            return True
    if "list-unsubscribe" in headers:
        return True
    body = _get_body(message).lower()
    return any(kw in body for kw in NEWSLETTER_KEYWORDS)


def _build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_unread_emails(max_results: int = 10, smart_filter: bool = True) -> List[Dict]:
    logger.info("[GMAIL] Fetching unread emails…")
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured. Visit /auth/google first.")
    try:
        service = _build_service(creds)
        result = service.users().messages().list(
            userId="me", q="is:unread in:inbox", maxResults=max_results * 3
        ).execute()

        emails: List[Dict] = []
        for msg in result.get("messages", []):
            if len(emails) >= max_results:
                break
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
            if smart_filter and _is_newsletter(full):
                continue
            hdrs = {
                h["name"].lower(): h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            emails.append({
                "id": msg["id"],
                "from": hdrs.get("from", "Unknown"),
                "subject": hdrs.get("subject", "(No subject)"),
                "date": hdrs.get("date", ""),
                "snippet": full.get("snippet", ""),
                "body": _get_body(full)[:500],
            })
        logger.info(f"[GMAIL] Returned {len(emails)} unread emails")
        return emails
    except HttpError as e:
        logger.error(f"[GMAIL] Error: {type(e).__name__}: {e}")
        raise


async def send_email(to: str, subject: str, body: str, cc: Optional[str] = None) -> None:
    logger.info(f"[GMAIL] Sending to: {to}, subject: {subject}")
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")
    try:
        service = _build_service(creds)
        msg = email_lib.message.EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        logger.info(f"[GMAIL] Email sent: id={result.get('id')} threadId={result.get('threadId')}")
    except HttpError as e:
        logger.error(f"[GMAIL] Send error: {type(e).__name__}: {e}")
        raise


async def move_to_folder(message_id: str, folder: str) -> None:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")
    try:
        service = _build_service(creds)
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
        label_id = next((l["id"] for l in labels if l["name"].lower() == folder.lower()), None)
        if not label_id:
            created = service.users().labels().create(userId="me", body={"name": folder}).execute()
            label_id = created["id"]
        service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()
    except HttpError as e:
        logger.error(f"[GMAIL] Move error: {type(e).__name__}: {e}")
        raise


async def mark_as_read(message_id: str) -> None:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")
    try:
        service = _build_service(creds)
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    except HttpError as e:
        logger.error(f"[GMAIL] Mark-read error: {type(e).__name__}: {e}")
        raise


async def search_emails(query: str, since_days: int = 7, max_results: int = 10) -> List[Dict]:
    logger.info(f"[GMAIL] Searching emails: {query!r} (last {since_days}d)")
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")
    try:
        service = _build_service(creds)
        result = service.users().messages().list(
            userId="me", q=f"{query} newer_than:{since_days}d", maxResults=max_results
        ).execute()
        emails: List[Dict] = []
        for msg in result.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="full"
            ).execute()
            hdrs = {
                h["name"].lower(): h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            emails.append({
                "id": msg["id"],
                "from": hdrs.get("from", "Unknown"),
                "subject": hdrs.get("subject", "(No subject)"),
                "date": hdrs.get("date", ""),
                "snippet": full.get("snippet", ""),
                "body": _get_body(full)[:800],
            })
        logger.info(f"[GMAIL] Search returned {len(emails)} emails")
        return emails
    except HttpError as e:
        logger.error(f"[GMAIL] Search error: {type(e).__name__}: {e}")
        raise


async def get_emails_awaiting_reply(hours_threshold: int = 6) -> List[Dict]:
    creds = await get_credentials()
    if not creds:
        return []
    try:
        service = _build_service(creds)
        query = f"is:unread in:inbox older_than:{hours_threshold}h" if hours_threshold > 0 else "is:unread in:inbox"
        result = service.users().messages().list(userId="me", q=query, maxResults=5).execute()
        emails: List[Dict] = []
        for msg in result.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            if _is_newsletter(full):
                continue
            hdrs = {
                h["name"].lower(): h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            emails.append({
                "id": msg["id"],
                "from": hdrs.get("from", "Unknown"),
                "subject": hdrs.get("subject", "(No subject)"),
                "date": hdrs.get("date", ""),
            })
        return emails
    except Exception as e:
        logger.error(f"[GMAIL] Awaiting-reply error: {type(e).__name__}: {e}")
        return []


async def get_recent_urgent_emails(minutes: int = 35) -> List[Dict]:
    """Return unread emails from the last N minutes with urgent subjects."""
    creds = await get_credentials()
    if not creds:
        return []
    urgent_keywords = ["דחוף", "urgent", "asap", "חשוב", "מיידי", "emergency", "immediately"]
    try:
        service = _build_service(creds)
        result = service.users().messages().list(
            userId="me", q=f"is:unread newer_than:{minutes}m in:inbox", maxResults=10
        ).execute()
        emails: List[Dict] = []
        for msg in result.get("messages", []):
            full = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            hdrs = {
                h["name"].lower(): h["value"]
                for h in full.get("payload", {}).get("headers", [])
            }
            subject = hdrs.get("subject", "")
            if any(kw.lower() in subject.lower() for kw in urgent_keywords):
                emails.append({"from": hdrs.get("from", ""), "subject": subject})
        return emails
    except Exception as e:
        logger.error(f"[GMAIL] Urgent email check error: {type(e).__name__}: {e}")
        return []
