import json
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import pandas as pd
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from app.services.f1_service import get_event_schedule, get_session

logger = logging.getLogger(__name__)

LIVETIMING_SESSION_INFO_URL = "https://livetiming.formula1.com/static/SessionInfo.json"

# Cached archive status to avoid hitting F1 API on every next-session call
_archive_status_cache: dict = {"status": None, "checked_at": 0}

# SessionInfo.SessionStatus values that indicate an active session
LIVE_STATUSES = {"Started", "Aborted"}

router = APIRouter()


def _find_round_for_location(location: str) -> int:
    """Look up the round number for a location from the current year's schedule."""
    if not location:
        return 0
    try:
        current_year = datetime.now(timezone.utc).year
        schedule = get_event_schedule(current_year, include_testing=True)
        location_lower = location.lower()
        for _, event in schedule.iterrows():
            event_location = str(event.get("Location", "")).lower()
            if event_location == location_lower:
                round_num = event.get("RoundNumber")
                return int(round_num) if pd.notna(round_num) else 0
    except Exception as e:
        logger.warning(f"Failed to find round for location '{location}': {e}")
    return 0


def _parse_session_date(event, session_index: int) -> datetime | None:
    """Extract timezone-naive UTC datetime for a session from the schedule."""
    date_utc_key = f"Session{session_index}DateUtc"
    date_key = f"Session{session_index}Date"

    session_date = None
    if date_utc_key in event.keys() and pd.notna(event[date_utc_key]):
        session_date = pd.to_datetime(event[date_utc_key])
    elif date_key in event.keys() and pd.notna(event[date_key]):
        session_date = pd.to_datetime(event[date_key])

    if session_date is not None and session_date.tzinfo is not None:
        session_date = session_date.replace(tzinfo=None)

    return session_date


def _is_session_archived() -> bool:
    """Check if the current F1 session archive is complete.

    Caches the result for 60s to avoid hammering the F1 API.
    """
    import time, requests
    now = time.time()
    if now - _archive_status_cache["checked_at"] < 60:
        return _archive_status_cache["status"] == "Complete"
    try:
        resp = requests.get(LIVETIMING_SESSION_INFO_URL, timeout=3)
        text = resp.text
        if text.startswith('\ufeff'):
            text = text[1:]
        data = json.loads(text)
        status = (data.get("ArchiveStatus") or {}).get("Status", "")
        _archive_status_cache["status"] = status
        _archive_status_cache["checked_at"] = now
        return status == "Complete"
    except Exception:
        return False


@router.get("/schedule/next-session")
def get_next_session():
    """Get the next upcoming F1 session for countdown display."""
    try:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        current_year = now.year

        # Get schedule including testing events
        schedule = get_event_schedule(current_year, include_testing=True)

        for _, event in schedule.iterrows():
            for i in range(1, 6):
                session_key = f"Session{i}"
                if session_key not in event.keys() or pd.isna(event[session_key]):
                    continue
                session_name = event[session_key]
                if str(session_name) == 'None':
                    continue
                session_date = _parse_session_date(event, i)
                if session_date is None:
                    continue

                # Skip sessions that ended (started more than 3h ago, or
                # archive is complete for the most recent session)
                if session_date <= now - timedelta(hours=3):
                    continue
                # If session already started, check if it's finished
                if session_date <= now:
                    if _is_session_archived():
                        continue

                is_testing = event.get('EventFormat') == 'testing'
                display_name = session_name
                if is_testing:
                    display_name = f"Day {i}"

                return {
                    "event_name": event["EventName"],
                    "session_name": display_name,
                    "session_type": session_name,
                    "session_date": session_date.isoformat() + "Z",
                    "round": int(event["RoundNumber"]) if pd.notna(event.get("RoundNumber")) else 0,
                    "is_testing": is_testing,
                    "country": event.get("Country", ""),
                    "location": event.get("Location", ""),
                }

        # No upcoming sessions found
        raise HTTPException(status_code=404, detail="No upcoming sessions found")

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/schedule/live-session")
async def get_live_session():
    """Check F1 livetiming API for an active session.

    Fetches SessionInfo.json from the F1 static API.
    Returns session info if a session is currently live, or 204 if not.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(LIVETIMING_SESSION_INFO_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=502, detail="F1 livetiming API unavailable")
                text = await resp.text()
                if text.startswith('\ufeff'):
                    text = text[1:]

        data = json.loads(text)

        session_status = data.get("SessionStatus", "")
        archive_status = (data.get("ArchiveStatus") or {}).get("Status", "")

        # A session is live if the archive is being generated (stream active)
        # or status is Started/Aborted. Once archived ("Complete"), it's over.
        is_live = (archive_status == "Generating"
                   or (session_status in LIVE_STATUSES and archive_status != "Complete"))

        if not is_live:
            return Response(status_code=204)

        meeting = data.get("Meeting", {})
        session_type = data.get("Type", "")
        session_name = data.get("Name", "")
        location = meeting.get("Location", "")

        # Determine page route
        page = "practice"
        lower_type = session_type.lower()
        if "qualifying" in lower_type or "shootout" in lower_type:
            page = "qualifying"
        elif lower_type in ("race", "sprint"):
            page = "race"

        # Look up round number from schedule
        round_number = _find_round_for_location(location)

        return {
            "is_live": True,
            "event_name": meeting.get("OfficialName", meeting.get("Name", "")),
            "session_name": session_name,
            "session_type": session_type,
            "location": location,
            "round": round_number,
            "meeting_key": meeting.get("Key"),
            "session_key": data.get("Key"),
            "country": (meeting.get("Country") or {}).get("Name", ""),
            "page": page,
            "path": data.get("Path", ""),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Failed to check F1 livetiming API: {e}")
        raise HTTPException(status_code=502, detail="Failed to check F1 livetiming API")


@router.get("/years")
def get_available_years():
    """Get list of available years with F1 data (2018 to current year)."""
    current_year = datetime.now().year
    years = list(range(current_year, 2017, -1))
    return {"years": years}


@router.get("/schedule/{year}")
def get_schedule(year: int):
    """Get the race schedule for a given year, including per-session
    name + UTC start time. Used by the home page to render event cards
    + click-to-expand session sub-cards."""
    try:
        schedule = get_event_schedule(year)
        events = []
        for _, event in schedule.iterrows():
            sessions = []
            for i in range(1, 6):
                name = event.get(f"Session{i}")
                if not name or (isinstance(name, float) and pd.isna(name)):
                    continue
                d = event.get(f"Session{i}DateUtc")
                sessions.append({
                    "index": i,
                    "name": str(name),
                    "date_utc": str(d) if d is not None and not pd.isna(d) else None,
                })
            events.append({
                "round": int(event["RoundNumber"]),
                "name": event["EventName"],
                "country": event["Country"],
                "location": event["Location"],
                "date": str(event["EventDate"]),
                "format": str(event.get("EventFormat", "")),
                "sessions": sessions,
            })
        return {"year": year, "events": events}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/results/{year}/{race}")
def get_race_results(year: int, race: str | int):
    """Get race results for a specific race."""
    try:
        session = get_session(year, race, "R")
        results = session.results
        return {
            "year": year,
            "race": race,
            "results": results[["Position", "DriverNumber", "Abbreviation",
                               "TeamName", "Status"]].to_dict(orient="records")
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
