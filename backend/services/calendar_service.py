import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pytz
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from services.gmail_service import get_credentials

logger = logging.getLogger(__name__)

# Simple in-process cache for calendar list (10 min TTL)
_cal_cache: List[Dict] = []
_cal_cache_ts: float = 0.0
_CAL_CACHE_TTL = 600


def _tz() -> pytz.BaseTzInfo:
    return pytz.timezone(settings.timezone)


async def get_all_calendars(force_refresh: bool = False) -> List[Dict]:
    """Return all calendars the user can write to, with 10-minute caching."""
    global _cal_cache, _cal_cache_ts
    if not force_refresh and _cal_cache and (time.time() - _cal_cache_ts) < _CAL_CACHE_TTL:
        return _cal_cache

    creds = await get_credentials()
    if not creds:
        return []
    try:
        service = build("calendar", "v3", credentials=creds)
        items = service.calendarList().list().execute().get("items", [])
        _cal_cache = [
            {
                "id": c["id"],
                "name": c.get("summary", c["id"]),
                "primary": c.get("primary", False),
                "writable": c.get("accessRole", "") in ("owner", "writer"),
            }
            for c in items
        ]
        _cal_cache_ts = time.time()
        logger.info(f"[CALENDAR] Loaded {len(_cal_cache)} calendars")
    except Exception as e:
        logger.error(f"[CALENDAR] Failed to load calendar list: {e}")
    return _cal_cache


async def get_calendar_list_for_prompt() -> str:
    """Format writable calendar list for injection into the system prompt."""
    cals = await get_all_calendars()
    writable = [c for c in cals if c["writable"]]
    if not writable:
        return ""
    lines = ["יומנים זמינים לכתיבה:"]
    for c in writable:
        tag = " ← ראשי" if c["primary"] else ""
        lines.append(f'  • "{c["name"]}"  id={c["id"]!r}{tag}')
    return "\n".join(lines)


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
    calendar_id: str = "primary",
) -> Dict:
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    logger.info(f"[CALENDAR] Creating event: {title!r} on {date} {start_time}-{end_time} calendar={calendar_id!r}")
    try:
        service = build("calendar", "v3", credentials=creds)
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": f"{date}T{start_time}:00", "timeZone": settings.timezone},
            "end": {"dateTime": f"{date}T{end_time}:00", "timeZone": settings.timezone},
        }
        created = service.events().insert(calendarId=calendar_id, body=body).execute()
        logger.info(f"[CALENDAR] Event created: id={created['id']} link={created.get('htmlLink')}")
        return {"id": created["id"], "title": title, "link": created.get("htmlLink", "")}
    except HttpError as e:
        logger.error(f"[CALENDAR] API error creating event: {type(e).__name__}: {e}")
        raise


async def delete_event(event_id: str, calendar_id: str = "primary") -> None:
    """Delete an event by ID from the specified calendar."""
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    logger.info(f"[CALENDAR] Deleting event id={event_id!r} from calendar={calendar_id!r}")
    try:
        service = build("calendar", "v3", credentials=creds)
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info(f"[CALENDAR] Event {event_id!r} deleted")
    except HttpError as e:
        logger.error(f"[CALENDAR] Delete error: {type(e).__name__}: {e}")
        raise


async def update_event(
    event_id: str,
    calendar_id: str = "primary",
    title: Optional[str] = None,
    date: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    description: Optional[str] = None,
    location: Optional[str] = None,
) -> Dict:
    """Patch an existing calendar event (only supplied fields are changed)."""
    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    patch: Dict = {}
    if title:
        patch["summary"] = title
    if description is not None:
        patch["description"] = description
    if location is not None:
        patch["location"] = location
    if date and start_time:
        patch["start"] = {"dateTime": f"{date}T{start_time}:00", "timeZone": settings.timezone}
    if date and end_time:
        patch["end"] = {"dateTime": f"{date}T{end_time}:00", "timeZone": settings.timezone}

    if not patch:
        return {"id": event_id, "note": "nothing to update"}

    logger.info(f"[CALENDAR] Updating event id={event_id!r} patch={patch}")
    try:
        service = build("calendar", "v3", credentials=creds)
        updated = service.events().patch(
            calendarId=calendar_id, eventId=event_id, body=patch
        ).execute()
        logger.info(f"[CALENDAR] Event updated: id={updated['id']}")
        return {"id": updated["id"], "title": updated.get("summary", "")}
    except HttpError as e:
        logger.error(f"[CALENDAR] Update error: {type(e).__name__}: {e}")
        raise


async def move_event(
    event_id: str,
    source_calendar_id: str = "primary",
    destination_calendar_id: str = "",
) -> Dict:
    """Move an event to a different calendar using events().move()."""
    if not destination_calendar_id:
        raise ValueError("destination_calendar_id is required")

    creds = await get_credentials()
    if not creds:
        raise ValueError("Google credentials not configured.")

    logger.info(
        f"[CALENDAR] Moving event {event_id!r} from {source_calendar_id!r} "
        f"to {destination_calendar_id!r}"
    )
    try:
        service = build("calendar", "v3", credentials=creds)
        moved = service.events().move(
            calendarId=source_calendar_id,
            eventId=event_id,
            destination=destination_calendar_id,
        ).execute()
        logger.info(f"[CALENDAR] Event moved: id={moved['id']}")
        return {"id": moved["id"], "title": moved.get("summary", "")}
    except HttpError as e:
        logger.error(f"[CALENDAR] Move error: {type(e).__name__}: {e}")
        raise
