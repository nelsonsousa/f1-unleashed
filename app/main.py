import os
import json
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from app import settings
from app.routers import livetiming, livetiming_stream, auth, races, weather, settings as settings_router
from app.logging_config import setup_logging
from app.version import get_version, check_latest_release
from app.services.auth_service import auth_service
from app.services.live_capture import live_capture, kill_orphan_ffmpeg
from app.services.weather_radar import radar_capture
from app.services.weather_forecast import forecast_capture

# Initialize logging
setup_logging()
logger = logging.getLogger(__name__)

# Track active live capture sessions
_active_live_capture: dict = {"session_id": None, "event_name": None, "session_type": None}

# Track sent notifications to avoid duplicates (key: "event_session_hours")
_sent_notifications: set = set()

# Post-CHEQUERED safeguard: { session_id: wall-clock datetime first seen }.
# Captures auto-stop 15 min after the chequered flag is shown, even if F1
# hasn't flipped ArchiveStatus to Complete yet.
_chequered_first_seen: dict = {}
POST_CHEQUERED_GRACE = timedelta(minutes=15)


async def _chequered_grace_expired(session_id: str, now_utc: datetime) -> bool:
    """True when the active capture has been past CHEQUERED for > 15 min.

    Reads the latest trackStatus from the capture's session.db (written
    live by the capture's preprocessor). Returns False on any error so a
    flaky DB read can't accidentally kill a live capture.
    """
    import sqlite3
    from app.processing.database import transient_db_path
    capture = live_capture._captures.get(session_id)
    if not capture:
        return False
    cache_path = capture.get("cache_path")
    if not cache_path:
        return False
    db_path = transient_db_path(cache_path)   # live DB is the transient scratch file
    if not db_path.exists():
        return False
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "SELECT data FROM messages WHERE topic='trackStatus' "
            "ORDER BY offset_ms DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        if conn is not None:
            conn.close()   # closed even if execute raises (M6)
    if not row:
        _chequered_first_seen.pop(session_id, None)
        return False
    try:
        latest_status = json.loads(row[0]).get("status")
    except (ValueError, TypeError, AttributeError):
        latest_status = None
    if latest_status != "finished":
        # Track returned to GREEN/etc — reset the timer (e.g. quali Q1
        # CHEQUERED followed by Q2 GREEN cancels this safeguard until
        # the final chequered flag).
        _chequered_first_seen.pop(session_id, None)
        return False
    if session_id not in _chequered_first_seen:
        _chequered_first_seen[session_id] = now_utc
        return False
    return (now_utc - _chequered_first_seen[session_id]) >= POST_CHEQUERED_GRACE


    # Token expiry checking is handled by live_session_monitor at milestone intervals.


from app.notifications import send_notification  # noqa: E402  (re-exported)


def get_session_duration_hours(session_type: str, is_testing: bool) -> int:
    """Get expected session duration in hours."""
    if is_testing:
        return 12  # Pre-season testing runs morning + afternoon
    if "practice" in session_type.lower():
        return 6
    return 3


async def live_session_monitor():
    """Background task to monitor for live sessions and auto-start capture.

    Adaptive live check interval based on proximity to next session:
    - >2h away:  every 60 min
    - 1-2h away: every 5 min
    - <1h away:  every 60s

    Also:
    - Refreshes schedule every hour to detect changes.
    - Checks token expiry once every 24h. Only alerts if token expires
      within 24h AND next session is more than 18h away.
    """
    import aiohttp

    SCHEDULE_REFRESH_INTERVAL = 60 * 60       # 1h — refresh schedule

    # The monitor calls this server's own API; honour the configured port.
    api_base = f"http://localhost:{os.environ.get('PORT', '1950')}/api/v1"

    last_schedule_refresh = 0
    cached_next_session = None    # {event_name, session_type, session_date, is_testing}

    # Wait for server to be ready before first check
    await asyncio.sleep(5)

    while True:
        try:
            try:
                now_utc = datetime.now(timezone.utc)
                async with aiohttp.ClientSession() as http_session:

                    # ── Refresh schedule cache (every hour) ──
                    sched_elapsed = (now_utc.timestamp() - last_schedule_refresh) \
                        if last_schedule_refresh else SCHEDULE_REFRESH_INTERVAL + 1

                    if sched_elapsed >= SCHEDULE_REFRESH_INTERVAL:
                        last_schedule_refresh = now_utc.timestamp()
                        async with http_session.get(
                            f"{api_base}/schedule/next-session"
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                date_str = data.get("session_date")
                                if date_str:
                                    cached_next_session = {
                                        "event_name": data.get("event_name", "Unknown"),
                                        "session_type": data.get("session_type", "Unknown"),
                                        "session_date": datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc),
                                        "is_testing": data.get("is_testing", False),
                                    }

                    # ── Live session check (every cycle) ──
                    # The /live-session endpoint returns:
                    #   200 + JSON          → session is live
                    #   204 (No Content)    → session is definitively NOT live
                    #   5xx / network error → F1 API transient failure
                    # Only the 204 case should stop an active capture; a
                    # transient 5xx must NOT trigger a stop+restart cycle
                    # (each restart truncates audio and disrupts the engine).
                    async with http_session.get(
                        f"{api_base}/schedule/live-session"
                    ) as live_resp:
                        if live_resp.status >= 500:
                            logger.warning(
                                f"live-session check returned {live_resp.status}; "
                                f"keeping current capture state unchanged"
                            )
                        elif live_resp.status == 200:
                            live_data = await live_resp.json()
                            live_event = live_data.get("event_name", "Unknown")
                            live_type = live_data.get("session_type", "Unknown")
                            notification_key = f"{live_event}_{live_type}"

                            # Notify once per session (gated by settings, card 27)
                            if f"{notification_key}_started" not in _sent_notifications:
                                if settings.get("ntfy.sessionLive", True):
                                    send_notification(
                                        "F1 Session LIVE",
                                        f"{live_event} - {live_type} is now LIVE!",
                                        priority="urgent",
                                        tags="checkered_flag,formula1"
                                    )
                                _sent_notifications.add(f"{notification_key}_started")

                            # Check if capture needs (re)starting
                            need_start = False
                            if (_active_live_capture["event_name"] != live_event or
                                    _active_live_capture["session_type"] != live_type):
                                need_start = True
                            elif _active_live_capture["session_id"]:
                                # Same session — check the capture's health.
                                try:
                                    info = live_capture.get_status(
                                        _active_live_capture["session_id"])
                                    if info["status"] == "error":
                                        logger.info(
                                            f"Capture {_active_live_capture['session_id']} "
                                            f"errored, restarting")
                                        need_start = True
                                    # "completed" = the SignalR feed closed = session
                                    # over. Do NOT restart: F1 can still report a
                                    # just-ended session as live while ArchiveStatus
                                    # lags, and restarting re-captures post-session —
                                    # rotating the real audio to commentary.NNN.aac
                                    # and recording junk (card xJAG0l4A). Red-flag
                                    # data gaps never reach "completed" (SignalR owns
                                    # reconnection, so is_alive stays true).
                                except (ValueError, KeyError):
                                    need_start = True

                            if need_start:
                                live_location = live_data.get("location", "")
                                live_round = live_data.get("round", 0)
                                live_meeting_key = live_data.get("meeting_key") or 0
                                live_session_key = live_data.get("session_key") or 0
                                live_session_name = live_data.get("session_name", live_type)
                                logger.info(f"Session live, starting capture: {live_event} - {live_type} (round {live_round}, meeting_key {live_meeting_key}, session_key {live_session_key}, {live_location})")
                                try:
                                    session_id = await live_capture.start_live(
                                        year=now_utc.year,
                                        meeting_name=live_location or live_event.replace(" ", "_"),
                                        session_type=live_type,
                                        round_number=live_round,
                                        meeting_key=live_meeting_key,
                                        session_name=live_session_name,
                                        session_key=live_session_key,
                                    )
                                    _active_live_capture["session_id"] = session_id
                                    _active_live_capture["event_name"] = live_event
                                    _active_live_capture["session_type"] = live_type
                                    logger.info(f"Live capture started: {session_id}")
                                except Exception as e:
                                    logger.error(f"Failed to start live capture: {e}")

                            # Post-CHEQUERED safeguard: 15 min after the
                            # chequered flag, force-stop even if F1 hasn't
                            # flipped ArchiveStatus to Complete.
                            sid = _active_live_capture["session_id"]
                            if sid and await _chequered_grace_expired(sid, now_utc):
                                logger.info(
                                    f"Capture {sid}: 15 min since CHEQUERED — "
                                    f"force-stopping (F1 ArchiveStatus still not Complete)"
                                )
                                try:
                                    await live_capture.stop(sid)
                                except Exception:
                                    pass
                                _active_live_capture["session_id"] = None
                                _active_live_capture["event_name"] = None
                                _active_live_capture["session_type"] = None
                                _chequered_first_seen.pop(sid, None)

                        elif live_resp.status == 204:
                            # Definitive "no session live" — safe to stop.
                            if _active_live_capture["session_id"]:
                                logger.info("Session ended (204), stopping capture")
                                try:
                                    await live_capture.stop(_active_live_capture["session_id"])
                                except Exception:
                                    pass
                                _active_live_capture["session_id"] = None
                                _active_live_capture["event_name"] = None
                                _active_live_capture["session_type"] = None
                                _sent_notifications.clear()
                            # Stop radar + forecast 5 minutes after session ends.
                            stop_when = datetime.now(timezone.utc) + timedelta(minutes=5)
                            if radar_capture.active:
                                radar_capture.schedule_stop(stop_when)
                            if forecast_capture.active:
                                forecast_capture.schedule_stop(stop_when)
                        else:
                            # 4xx (e.g. 502 from upstream) — treat as transient.
                            logger.warning(
                                f"live-session check returned {live_resp.status}; "
                                f"keeping current capture state unchanged"
                            )

                    # ── Weather radar lifecycle ──
                    # Start radar 15 min before the session begins; the
                    # 204 branch above schedules the 5-min-after stop.
                    # The 4 h trailing window is a safety net in case
                    # we never see a 204 (network issues, server restart).
                    if cached_next_session:
                        s = cached_next_session
                        radar_window_start = s["session_date"] - timedelta(minutes=15)
                        radar_window_end = s["session_date"] + timedelta(hours=4)
                        # Both start only once the live feed is up — so the F1 keys are known and
                        # the session cache dir exists. Tiles co-locate in that session dir.
                        live_sid = _active_live_capture["session_id"]
                        cache_path = live_capture.cache_path_for(live_sid) if live_sid else None
                        # Forecast + current conditions: begin as soon as the session data STREAM
                        # begins (live feed up), NOT only from 15-min-before — otherwise the
                        # opening minutes of the session have no forecast/condition coverage and
                        # the tile shows nothing. minutely_15 is quarter-hour aligned, so the first
                        # capture already covers from the marker containing the stream. (user)
                        if (cache_path and now_utc <= radar_window_end
                                and forecast_capture.active_key != str(cache_path)):
                            forecast_capture.start(
                                session_dir=cache_path,
                                meeting_name=s["event_name"],
                                stop_at=radar_window_end,
                            )
                        # Radar stays gated to the 15-min-before → end window (Rainbow.ai call budget).
                        if (radar_window_start <= now_utc <= radar_window_end and cache_path
                                and radar_capture.active_key != str(cache_path)):
                            radar_capture.start(
                                session_dir=cache_path,
                                meeting_name=s["event_name"],
                                stop_at=radar_window_end,
                            )

                    # ── Pre-session notifications (gated + lead from settings, card 27) ──
                    if cached_next_session and settings.get("ntfy.preSession", True):
                        s = cached_next_session
                        hours_until = (s["session_date"] - now_utc).total_seconds() / 3600
                        lead_min = float(settings.get("ntfy.preSessionLeadMinutes", 60) or 60)
                        mins_until = hours_until * 60

                        # Notify `lead_min` minutes before the session starts.
                        if 0 < mins_until <= lead_min:
                            notify_key = f"{s['event_name']}_{s['session_type']}_presession"
                            if notify_key not in _sent_notifications:
                                send_notification(
                                    "F1 Session Starting Soon",
                                    f"{s['event_name']} - {s['session_type']} starts in {int(mins_until)} minutes",
                                    priority="high",
                                    tags="clock,formula1"
                                )
                                _sent_notifications.add(notify_key)

                    # ── Token expiry check (at milestone intervals before session) ──
                    # Only check when next session is <24h away.
                    # Notify at 24h, 12h, 6h, 3h, 2h, 1h milestones.
                    # Alert if not authenticated, or token expires within T+6h
                    # (where T = hours until session).
                    LOGIN_MILESTONES = [24, 12, 6, 3, 2, 1]

                    if cached_next_session and settings.get("ntfy.tokenExpiry", True):
                        s = cached_next_session
                        hours_until = (s["session_date"] - now_utc).total_seconds() / 3600

                        if 0 < hours_until <= 24:
                            milestone = None
                            for m in LOGIN_MILESTONES:
                                if hours_until <= m:
                                    milestone = m

                            if milestone is not None:
                                notify_key = f"{s['event_name']}_{s['session_type']}_login_{milestone}h"
                                if notify_key not in _sent_notifications:
                                    auth_status = auth_service.get_status()

                                    if not auth_status.is_authenticated:
                                        send_notification(
                                            "F1 Login Required",
                                            f"{s['event_name']} - {s['session_type']} in {hours_until:.0f}h.\n\nNot logged in. Please log in to capture live timing.",
                                            priority="urgent" if hours_until <= 3 else "high",
                                            tags="warning,formula1"
                                        )
                                        _sent_notifications.add(notify_key)
                                    elif (auth_status.expires_in_hours is not None
                                          and auth_status.expires_in_hours <= hours_until + 6):
                                        if auth_status.expires_in_hours < hours_until:
                                            msg = f"{s['event_name']} - {s['session_type']} in {hours_until:.0f}h.\n\nToken expires in {auth_status.expires_in_hours:.0f}h — before the session. Please re-login."
                                        else:
                                            msg = f"{s['event_name']} - {s['session_type']} in {hours_until:.0f}h.\n\nToken expires in {auth_status.expires_in_hours:.0f}h. Please re-login."
                                        send_notification(
                                            "F1 Login Expiring",
                                            msg,
                                            priority="urgent" if hours_until <= 3 else "high",
                                            tags="warning,formula1"
                                        )
                                        _sent_notifications.add(notify_key)

            except aiohttp.ClientError as e:
                logger.debug(f"Could not check for live session: {e}")

            # ── Adaptive sleep interval ──
            sleep_interval = 60 * 60  # default: 1 hour
            if cached_next_session:
                hours_until = (cached_next_session["session_date"] - datetime.now(timezone.utc)).total_seconds() / 3600
                if hours_until <= 1:
                    sleep_interval = 60       # <1h: every 60s
                elif hours_until <= 2:
                    sleep_interval = 5 * 60   # 1-2h: every 5 min

            await asyncio.sleep(sleep_interval)

        except asyncio.CancelledError:
            logger.info("Live session monitor stopped")
            break
        except Exception as e:
            logger.error(f"Error in live session monitor: {e}")
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - start background tasks on startup."""
    # Sweep up any ffmpeg children left behind by a previous server
    # instance so they don't race the new capture on the same .aac file.
    kill_orphan_ffmpeg()

    # Start background tasks
    live_monitor_task = asyncio.create_task(live_session_monitor())
    logger.info("Started live session monitor")

    # No startup DB backfill: session DBs are transient scratch files built on
    # demand (engine connect / live capture) and deleted when no longer viewed.

    yield

    # Cleanup on shutdown
    live_monitor_task.cancel()
    try:
        await live_monitor_task
    except asyncio.CancelledError:
        pass

    # Stop any active live capture
    if _active_live_capture["session_id"]:
        try:
            await live_capture.stop(_active_live_capture["session_id"])
        except Exception:
            pass

    # Stop the background weather captures and close the shared HTTP session so
    # nothing is left running/open at shutdown (M6).
    for cap in (radar_capture, forecast_capture):
        try:
            cap.stop()
        except Exception:
            pass
    try:
        from app.services.livetiming_fetcher import livetiming_fetcher
        await livetiming_fetcher.close()
    except Exception:
        pass


app = FastAPI(
    title="Formula 1 Live Timing API",
    description="API for F1 Live Timing data replay and streaming",
    version=get_version(),
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory="templates")


def _asset(path: str) -> str:
    """Cache-busting static URL: /static/<path>?v=<file mtime>. Replaces the
    hand-bumped ?v=YYYY-MM-DD tags — the version follows the file automatically."""
    try:
        mtime = int((Path("static") / path).stat().st_mtime)
        return f"/static/{path}?v={mtime}"
    except OSError:
        return f"/static/{path}"


templates.env.globals["asset"] = _asset

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(races.router, prefix="/api/v1", tags=["races"])
app.include_router(livetiming.router, prefix="/api/v1", tags=["livetiming"])
app.include_router(livetiming_stream.router, prefix="/api/v1", tags=["livetiming-stream"])
app.include_router(weather.router, prefix="/api/v1", tags=["weather"])
app.include_router(settings_router.router, prefix="/api/v1", tags=["settings"])


@app.get("/")
def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# The in-app user guide (release v2.0.0) is split into four context pages: the
# main window + player, and one per session type. Sources live in docs/guide/.
GUIDE_PARTS = [
    ("main", "Main window"),
    ("practice", "Practice"),
    ("qualifying", "Qualifying"),
    ("race", "Race"),
]


@app.get("/help")
@app.get("/help/{part}")
def help_page(request: Request, part: str = "main"):
    """Render one part of the split user guide (docs/guide/{part}.md)."""
    import markdown as _md
    valid = {k for k, _ in GUIDE_PARTS}
    if part not in valid:
        part = "main"
    try:
        text = Path(f"docs/guide/{part}.md").read_text(encoding="utf-8")
        content = _md.markdown(
            text, extensions=["tables", "fenced_code", "toc", "sane_lists"])
    except OSError:
        content = "<p>Documentation not found.</p>"
    nav = [(k, label, k == part) for k, label in GUIDE_PARTS]
    return templates.TemplateResponse(
        "help.html", {"request": request, "content": content, "nav": nav})


@app.get("/api/v1/version")
def version_info():
    """Running app version + latest GitHub release (for the update indicator)."""
    return check_latest_release()


@app.get("/browser")
def browser():
    # Browser page folded into the home page; keep the route as a 301
    # redirect so any old bookmarks still land somewhere useful.
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=301)


def _session_page(request: Request, session: str, session_type: str):
    """Render the unified session page; session_type drives conditional includes."""
    return templates.TemplateResponse(
        "pages/session.html",
        {
            "request": request,
            "session_id": session,
            "session_type": session_type,
        },
    )


@app.get("/qualifying")
def qualifying_page(request: Request, session: str = None):
    return _session_page(request, session, "qualifying")


@app.get("/practice")
def practice_page(request: Request, session: str = None):
    return _session_page(request, session, "practice")


@app.get("/race")
def race_page(request: Request, session: str = None):
    return _session_page(request, session, "race")


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/api/v1/live-capture/status")
def get_live_capture_status():
    """Get the current live capture status."""
    if _active_live_capture["session_id"]:
        try:
            info = live_capture.get_status(_active_live_capture["session_id"])
            return {
                "active": True,
                "event_name": _active_live_capture["event_name"],
                "session_type": _active_live_capture["session_type"],
                "session_id": _active_live_capture["session_id"],
                "status": info.get("status"),
                "message_count": info.get("message_count", 0),
            }
        except Exception:
            pass

    return {
        "active": False,
        "event_name": None,
        "session_type": None,
        "session_id": None,
    }


@app.get("/favicon.ico")
def favicon():
    return FileResponse("static/favicon.svg", media_type="image/svg+xml")
