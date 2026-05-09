import logging

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from config import settings
from services.gmail_service import save_credentials

router = APIRouter()
logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
]

def _build_flow():
    """Build an OAuth flow from credentials.json (if present) or .env variables."""
    from google_auth_oauthlib.flow import Flow
    import os, json

    creds_path = os.path.join(os.path.dirname(__file__), "..", "credentials.json")
    if os.path.exists(creds_path):
        with open(creds_path) as f:
            raw = json.load(f)
        # Support both "installed" and "web" credential types
        key = "web" if "web" in raw else "installed"
        client_config = {
            "web": {
                **raw[key],
                # Ensure our redirect URI is listed
                "redirect_uris": raw[key].get("redirect_uris", []) + [settings.google_redirect_uri],
            }
        }
    else:
        client_config = {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [settings.google_redirect_uri],
            }
        }

    flow = Flow.from_client_config(client_config, scopes=SCOPES)
    flow.redirect_uri = settings.google_redirect_uri
    return flow


@router.get("/auth/google")
async def google_auth():
    """Redirect the user to Google's OAuth consent screen."""
    flow = _build_flow()
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return RedirectResponse(url=auth_url)


@router.get("/auth/google/callback")
async def google_callback(code: str):
    """Handle the OAuth callback, persist tokens, return success page."""
    try:
        flow = _build_flow()
        flow.fetch_token(code=code)
        creds = flow.credentials
        await save_credentials(creds, settings.google_client_id, settings.google_client_secret)
        logger.info("[AUTH] Google OAuth completed successfully.")
        return HTMLResponse(
            "<h2>✅ Google authentication successful!</h2>"
            "<p>You can close this tab and return to WhatsApp.</p>"
        )
    except Exception as e:
        logger.error(f"[AUTH] OAuth callback error: {e}")
        return HTMLResponse(f"<h2>❌ Authentication failed</h2><pre>{e}</pre>", status_code=400)


@router.get("/auth/status")
async def auth_status():
    """Return whether Google credentials are stored."""
    from services.gmail_service import get_credentials
    creds = await get_credentials()
    return {"google_authenticated": creds is not None and creds.valid}


@router.delete("/auth/google/revoke")
async def google_revoke():
    """Delete stored Google token so the user can re-authenticate cleanly."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from database import AsyncSessionLocal, Setting
    from datetime import datetime, timezone
    async with AsyncSessionLocal() as session:
        await session.execute(
            __import__("sqlalchemy").delete(Setting).where(Setting.key == "google_tokens")
        )
        await session.commit()
    logger.info("[AUTH] Google token revoked — re-auth required")
    return {"message": f"Token cleared. Visit {settings.backend_url}/auth/google to reconnect."}
