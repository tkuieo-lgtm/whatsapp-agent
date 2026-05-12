"""
Evolution API webhook receiver.

Translates Evolution API event format to the existing _handle_message pipeline.
Mirrors the filtering the old Baileys bridge (index.js) did locally:
  - Group messages: only processed when bot is @mentioned
  - DM messages: only from owner
  - Audio (ptt): fetched via /chat/getBase64FromMediaMessage
"""
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import IncomingMessage

router = APIRouter()
logger = logging.getLogger(__name__)

_seen: set = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(message: dict) -> str:
    return (
        message.get("conversation")
        or message.get("extendedTextMessage", {}).get("text")
        or message.get("imageMessage", {}).get("caption")
        or ""
    )


def _has_bot_mention(text: str, message: dict) -> bool:
    if f"@{settings.bot_name}".lower() in text.lower():
        return True
    mentions = (
        message.get("extendedTextMessage", {})
        .get("contextInfo", {})
        .get("mentionedJid", [])
    )
    return bool(mentions)


def _strip_bot_mention(text: str) -> str:
    return re.sub(rf"@{re.escape(settings.bot_name)}", "", text, flags=re.IGNORECASE).strip()


async def _fetch_audio_base64(key: dict) -> Optional[str]:
    """
    Fetch base64-encoded audio from Evolution API.
    Some webhook configs include base64 inline; otherwise use the API endpoint.
    """
    if not settings.evolution_api_url:
        return None
    url = (
        f"{settings.evolution_api_url.rstrip('/')}"
        f"/chat/getBase64FromMediaMessage/{settings.evolution_instance}"
    )
    headers = {"apikey": settings.evolution_api_key or ""}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json={"message": {"key": key}}, headers=headers)
            resp.raise_for_status()
            return resp.json().get("base64")
    except Exception as e:
        logger.error(f"[EVOLUTION] getBase64FromMediaMessage failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Core event handler
# ---------------------------------------------------------------------------

async def _handle_evolution_event(body: dict) -> None:
    event = body.get("event", "")
    if event != "messages.upsert":
        logger.debug(f"[EVOLUTION] Skipping event: {event}")
        return

    data     = body.get("data", {})
    key      = data.get("key", {})
    message  = data.get("message", {})
    msg_type = data.get("messageType", "")

    if key.get("fromMe"):
        return

    msg_id = key.get("id", "")
    if msg_id in _seen:
        return
    _seen.add(msg_id)
    if len(_seen) > 2000:
        _seen.clear()

    remote_jid   = key.get("remoteJid", "")
    is_group     = remote_jid.endswith("@g.us")
    participant  = key.get("participant", "")
    sender_jid   = participant if (is_group and participant) else remote_jid
    sender_phone = sender_jid.split("@")[0]

    logger.info(
        f"[EVOLUTION] event={event} type={msg_type} "
        f"jid={remote_jid} sender={sender_phone} is_group={is_group}"
    )

    # --- Group: only respond to @mentions ---
    if is_group:
        text = _extract_text(message)
        if not _has_bot_mention(text, message):
            return
        text = _strip_bot_mention(text)

    # --- DM: owner only ---
    else:
        from services.security_service import is_owner
        if not is_owner(sender_phone):
            logger.warning(f"[EVOLUTION] DM from non-owner {sender_phone} — ignored")
            return
        text = _extract_text(message)

    from routers.messages import _handle_message

    # Audio voice note (ptt)
    audio_info = message.get("audioMessage", {})
    if msg_type == "audioMessage" and audio_info.get("ptt"):
        b64 = audio_info.get("base64") or await _fetch_audio_base64(key)
        if not b64:
            logger.error("[EVOLUTION] Could not retrieve audio bytes — skipping")
            return
        await _handle_message(IncomingMessage(
            sender=settings.owner_phone,
            message="",
            is_group=is_group,
            group_id=remote_jid if is_group else None,
            group_sender=sender_phone if is_group else None,
            message_type="audio",
            media_data=b64,
            media_mime="audio/ogg; codecs=opus",
        ))
        return

    # Text / image caption
    if not text.strip():
        return
    await _handle_message(IncomingMessage(
        sender=settings.owner_phone,
        message=text,
        is_group=is_group,
        group_id=remote_jid if is_group else None,
        group_sender=sender_phone if is_group else None,
    ))


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@router.post("/webhook/evolution")
async def evolution_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receives all Evolution API webhook events."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    event    = body.get("event", "?")
    instance = body.get("instance", "?")
    logger.info(f"[EVOLUTION] Incoming: event={event} instance={instance}")

    background_tasks.add_task(_handle_evolution_event, body)
    return {"ok": True}
