import logging
from datetime import datetime, timedelta
from typing import Dict, List

import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from services.gmail_service import get_credentials

logger = logging.getLogger(__name__)


def _tz():
    return pytz.timezone(settings.timezone)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

async def get_todays_events() -> List[Dict]:
    return await _get_events(days=1)


async def get_weeks_events() -> List[Dict]:
    return await _get_events(days=7)


async def _get_events(days: int) -> List[Dict]:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured. Visit /auth/google first.")

    try:
        service = build("calendar", "v3", credentials=creds)
        tz = _tz()
        now = datetime.now(tz)
        # Use tz.localize on a naive midnight datetime to get the correct DST offset
        start = tz.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
        end = start + timedelta(days=days)

        result = service.events().list(
            calendarId="primary",
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events: List[Dict] = []
        for ev in result.get("items", []):
            start_dt = ev.get("start", {})
            time_str = (
                datetime.fromisoformat(start_dt["dateTime"])
                .astimezone(tz)
                .strftime("%H:%M")
                if "dateTime" in start_dt
                else "כל היום"
            )
            events.append({
                "id": ev["id"],
                "title": ev.get("summary", "(ללא כותרת)"),
                "time": time_str,
                "location": ev.get("location", ""),
                "description": (ev.get("description") or "")[:200],
            })
        return events
    except HttpError as e:
        logger.error(f"[CALENDAR] API error getting events: {e}")
        raise


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def create_event(
    title: str,
    date: str,
    start_time: str,
    end_time: str,
    description: str = "",
    location: str = "",
) -> Dict:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    try:
        service = build("calendar", "v3", credentials=creds)
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": f"{date}T{start_time}:00", "timeZone": settings.timezone},
            "end": {"dateTime": f"{date}T{end_time}:00", "timeZone": settings.timezone},
        }
        created = service.events().insert(calendarId="primary", body=body).execute()
        return {"id": created["id"], "title": title, "link": created.get("htmlLink", "")}
    except HttpError as e:
        logger.error(f"[CALENDAR] API error creating event: {e}")
        raise


async def delete_event(event_id: str) -> None:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId="primary", eventId=event_id).execute()
    except HttpError as e:
        logger.error(f"[CALENDAR] API error deleting event: {e}")
        raise
