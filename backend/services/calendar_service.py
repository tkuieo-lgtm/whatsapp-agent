import logging
from datetime import datetime, timedelta
from typing import Dict, List

import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from services.gmail_service import get_credentials

logger = logging.getLogger(__name__)


def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(settings.timezone)


def _today_midnight(tz: pytz.BaseTzInfo, offset_days: int = 0) -> datetime:
    """Return midnight of today (+ offset_days) in the configured timezone."""
    now = datetime.now(tz)
    base = tz.localize(datetime(now.year, now.month, now.day, 0, 0, 0))
    return base + timedelta(days=offset_days)


# ---------------------------------------------------------------------------
# Public read functions
# ---------------------------------------------------------------------------

async def get_todays_events() -> List[Dict]:
    return await _get_events(offset_days=0, days=1)


async def get_tomorrows_events() -> List[Dict]:
    return await _get_events(offset_days=1, days=1)


async def get_weeks_events() -> List[Dict]:
    return await _get_events(offset_days=0, days=7)


# ---------------------------------------------------------------------------
# Core query
# ---------------------------------------------------------------------------

async def _get_events(offset_days: int, days: int) -> List[Dict]:
    """
    Fetch events from ALL user calendars for the requested date window.
    All datetimes are anchored to Asia/Jerusalem (settings.timezone).
    Both timed (start.dateTime) and all-day/multi-day (start.date) events
    are returned.
    """
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured. Visit /auth/google first.")

    try:
        service = build("calendar", "v3", credentials=creds)
        tz = _tz()

        time_min = _today_midnight(tz, offset_days)
        time_max = time_min + timedelta(days=days)

        logger.info(
            f"[CALENDAR] query offset={offset_days} days={days} | "
            f"timeMin={time_min.isoformat()} | timeMax={time_max.isoformat()}"
        )

        # Fetch all calendars the user has access to
        cal_list = service.calendarList().list().execute().get("items", [])
        cal_ids = [c["id"] for c in cal_list]
        logger.info(
            f"[CALENDAR] querying {len(cal_ids)} calendars: "
            f"{[c.get('summary', c['id']) for c in cal_list]}"
        )

        # Query every calendar and merge results
        raw_events: List[Dict] = []
        for cal_id in cal_ids:
            try:
                result = service.events().list(
                    calendarId=cal_id,
                    timeMin=time_min.isoformat(),
                    timeMax=time_max.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                ).execute()
                raw_events.extend(result.get("items", []))
            except HttpError as e:
                logger.warning(f"[CALENDAR] Skipping calendar {cal_id}: {e}")

        # Sort merged list chronologically
        def _sort_key(ev: Dict) -> str:
            s = ev.get("start", {})
            return s.get("dateTime") or s.get("date") or ""

        raw_events.sort(key=_sort_key)

        events: List[Dict] = []
        seen_ids: set = set()
        for ev in raw_events:
            # De-duplicate (same event can appear in multiple calendars)
            ev_id = ev.get("id", "")
            if ev_id in seen_ids:
                continue
            seen_ids.add(ev_id)

            ev_start = ev.get("start", {})

            if "dateTime" in ev_start:
                # Timed event
                dt = datetime.fromisoformat(ev_start["dateTime"]).astimezone(tz)
                time_str = dt.strftime("%H:%M")
                date_str = dt.strftime("%Y-%m-%d")
            elif "date" in ev_start:
                # All-day or multi-day event
                time_str = "כל היום"
                date_str = ev_start["date"]  # YYYY-MM-DD as returned by API
            else:
                continue  # unknown format, skip

            events.append({
                "id": ev_id,
                "date": date_str,
                "time": time_str,
                "title": ev.get("summary", "(ללא כותרת)"),
                "location": ev.get("location", ""),
                "description": (ev.get("description") or "")[:200],
            })

        logger.info(f"[CALENDAR] returned {len(events)} events total")
        return events

    except HttpError as e:
        logger.error(f"[CALENDAR] API error: {e}")
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
