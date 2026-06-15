"""Tomorrow.io weather-radar capture and cache.

Pulls the `precipitationIntensity` map tile that covers each F1 circuit
on a schedule synchronised to the session window:

    session_start − 15 min   →   start capture
    session_end   + 5 min    →   stop capture
    refresh interval         =   3 min  (≤ 25 req/hr free-tier cap)

Tiles are persisted under ``data/weather_radar_cache/{year}/{event}/
{session_type}/{utc_iso}.png`` and served by ``/api/v1/weather/radar/
latest``. Live captures cache once; replays of the same session read
from disk and never re-fetch.

Get an API key from https://app.tomorrow.io/development/keys and set
``TOMORROW_API_KEY`` in ``.env`` (free tier is enough).
"""

import asyncio
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

from app.config import DATA_DIR

# Defensive — ensures TOMORROW_API_KEY is loaded even when this module
# is imported by standalone scripts that don't pull in auth_service.
load_dotenv()

logger = logging.getLogger(__name__)

# Weather-radar tile cache — moved out of the repo to the OS-appropriate data
# dir alongside the livetiming cache (card 25).
CACHE_DIR = DATA_DIR / "weather_radar_cache"
# One precipitation tile per refresh; a fetch every 5 min = 12 calls/hr,
# well under the 25/hr free-tier cap. The overlay is the rain radar only
# — overall sky condition is conveyed by the weather-condition icon
# (driven by /api/v1/weather), not a cloud-cover overlay.
REFRESH_INTERVAL_S = 300
TILE_ZOOM = 13             # ~3–5 km tile width at F1 latitudes
LAYERS = ("precipitationIntensity",)
TILE_EXT = "png"

# Free tier (Tomorrow.io): 500 req/day, 25 req/hour, 3 req/sec.
HOURLY_BUDGET = 25
DAILY_BUDGET = 500


CIRCUITS_FILE = Path("static/data/circuits.json")


def _load_track_locations() -> dict[str, tuple[float, float]]:
    """Build a name → (lat, lng) lookup from static/data/circuits.json.
    The canonical key is `event_name`; all values in
    `variants.event_name` are also registered so sessionInfo's
    `meetingName` matches regardless of which alias F1 sends."""
    if not CIRCUITS_FILE.exists():
        logger.warning("Circuits file not found at %s", CIRCUITS_FILE)
        return {}
    try:
        payload = json.loads(CIRCUITS_FILE.read_text())
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s", CIRCUITS_FILE, e)
        return {}
    out: dict[str, tuple[float, float]] = {}
    for c in payload.get("circuits", []):
        lat = c.get("latitude")
        lng = c.get("longitude")
        if lat is None or lng is None:
            continue
        coord = (float(lat), float(lng))
        names = [c.get("event_name")]
        names.extend(c.get("variants", {}).get("event_name", []) or [])
        for n in names:
            if isinstance(n, str) and n:
                out[n] = coord
    return out


# Circuit lat/lng lookup. Keyed by `meetingName` (sessionInfo) and its
# known variants from circuits.json. Loaded once at import time.
TRACK_LOCATIONS: dict[str, tuple[float, float]] = _load_track_locations()


def _slugify(name: str) -> str:
    """Filesystem-safe representation of a name."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "unknown"


def _lat_lng_to_tile(lat: float, lng: float, zoom: int) -> tuple[int, int]:
    """Standard slippy-map tile coordinates from WGS84 lat/lng."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lng + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


# Equatorial circumference of WGS84 in metres.
_EARTH_CIRC_M = 40_075_017.0


def tile_size_m(lat: float, zoom: int) -> float:
    """Horizontal extent of one Web Mercator tile, in metres, at the
    given latitude. (Vertically the tile covers the same metres at its
    centre latitude — they're square in pixel space.)"""
    return _EARTH_CIRC_M / (2 ** zoom) * math.cos(math.radians(lat))


def _api_key() -> Optional[str]:
    return os.getenv("TOMORROW_API_KEY")


def _cache_dir(year: int, event_name: str, session_type: str) -> Path:
    return CACHE_DIR / str(year) / _slugify(event_name) / _slugify(session_type)


def list_cached_tiles(year: int, event_name: str, session_type: str,
                       layer: str) -> list[Path]:
    """Return chronologically-sorted PNG files for a session+layer."""
    d = _cache_dir(year, event_name, session_type)
    if not d.exists():
        return []
    return sorted(d.glob(f"*_{layer}.{TILE_EXT}"))


def has_cached_weather(year: int, event_name: str, session_type: str) -> bool:
    """True if ANY weather-radar tile is cached for the session (any layer).
    Best-effort presence check for the home-page weather icon (card)."""
    d = _cache_dir(year, event_name, session_type)
    return d.exists() and any(d.glob(f"*.{TILE_EXT}"))


def latest_cached_tile(year: int, event_name: str, session_type: str,
                        layer: str) -> Optional[Path]:
    tiles = list_cached_tiles(year, event_name, session_type, layer)
    return tiles[-1] if tiles else None


def cached_tile_at(year: int, event_name: str, session_type: str,
                    layer: str, target_utc: datetime) -> Optional[Path]:
    """Return the cached tile whose filename UTC timestamp is closest to
    `target_utc`. Filenames are `YYYYMMDDTHHMMSSZ_<layer>.png`. Used by
    replays so the displayed rain pattern matches the playback clock —
    not whatever was captured last."""
    tiles = list_cached_tiles(year, event_name, session_type, layer)
    if not tiles:
        return None
    best, best_dt = None, None
    for t in tiles:
        stem = t.stem.split("_", 1)[0]  # "20260524T194519Z"
        try:
            ts = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dt = abs((ts - target_utc).total_seconds())
        if best_dt is None or dt < best_dt:
            best, best_dt = t, dt
    return best


class _TokenBucket:
    """Two-bucket rate limiter — hourly + daily. Non-blocking: callers
    ask `try_take()` and skip if it returns False."""

    def __init__(self, hourly: int, daily: int):
        self.hourly_cap = hourly
        self.daily_cap = daily
        self._hourly: list[float] = []  # epoch s of recent calls
        self._daily: list[float] = []

    def try_take(self) -> bool:
        now = datetime.now(timezone.utc).timestamp()
        self._hourly = [t for t in self._hourly if now - t < 3600]
        self._daily = [t for t in self._daily if now - t < 86400]
        if len(self._hourly) >= self.hourly_cap:
            return False
        if len(self._daily) >= self.daily_cap:
            return False
        self._hourly.append(now)
        self._daily.append(now)
        return True


_bucket = _TokenBucket(HOURLY_BUDGET, DAILY_BUDGET)


async def fetch_tile(meeting_name: str, layer: str) -> Optional[bytes]:
    """Fetch one map tile for a circuit. Returns PNG bytes, or None if
    rate-limited / missing key / unknown circuit / upstream failure."""
    key = _api_key()
    if not key:
        logger.warning("TOMORROW_API_KEY not set; skipping radar fetch")
        return None
    loc = TRACK_LOCATIONS.get(meeting_name)
    if not loc:
        logger.warning("No radar coords for meeting %r; skipping", meeting_name)
        return None
    if not _bucket.try_take():
        logger.info("Radar fetch rate-limited (hourly/daily cap reached); skipping")
        return None

    lat, lng = loc
    x, y = _lat_lng_to_tile(lat, lng, TILE_ZOOM)
    url = (
        f"https://api.tomorrow.io/v4/map/tile/{TILE_ZOOM}/{x}/{y}"
        f"/{layer}/now.{TILE_EXT}?apikey={key}"
    )
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except httpx.HTTPStatusError as e:
        logger.warning(
            "Tomorrow.io tile fetch failed (%s, layer=%s): %s",
            e.response.status_code, layer, e.response.text[:200],
        )
        return None
    except httpx.RequestError as e:
        logger.warning("Tomorrow.io tile request error: %s", e)
        return None


def save_tile(year: int, event_name: str, session_type: str,
              fetched_at: datetime, layer: str, data: bytes) -> Path:
    d = _cache_dir(year, event_name, session_type)
    d.mkdir(parents=True, exist_ok=True)
    iso = fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = d / f"{iso}_{layer}.{TILE_EXT}"
    out.write_bytes(data)
    return out


class WeatherRadarCapture:
    """Drives the periodic tile fetch for one active session. Idempotent
    start/stop; only one session can be active at a time."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._active_key: Optional[tuple[int, str, str]] = None
        self._stop_at: Optional[datetime] = None  # if set, loop exits after this

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def active_key(self) -> Optional[tuple[int, str, str]]:
        return self._active_key

    def start(self, year: int, event_name: str, session_type: str,
              stop_at: Optional[datetime] = None) -> None:
        if self.active and self._active_key == (year, event_name, session_type):
            # Already running for the same session — update stop_at if given.
            if stop_at is not None:
                self._stop_at = stop_at
            return
        if self.active:
            logger.info("Switching radar capture to %s %s (was %s)",
                        event_name, session_type, self._active_key)
            self._cancel()
        self._active_key = (year, event_name, session_type)
        self._stop_at = stop_at
        self._task = asyncio.create_task(self._loop())
        logger.info("Radar capture started for %s %s%s",
                    event_name, session_type,
                    f" (stop at {stop_at.isoformat()})" if stop_at else "")

    def stop(self) -> None:
        if not self.active:
            return
        logger.info("Radar capture stopping for %s", self._active_key)
        self._cancel()
        self._active_key = None
        self._stop_at = None

    def schedule_stop(self, when: datetime) -> None:
        """Set a deadline at which the loop will exit on its own."""
        if self.active:
            self._stop_at = when
            logger.info("Radar capture stop scheduled for %s", when.isoformat())

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                if self._stop_at and datetime.now(timezone.utc) >= self._stop_at:
                    logger.info("Radar capture reached scheduled stop")
                    break
                year, event_name, session_type = self._active_key
                now = datetime.now(timezone.utc)
                for layer in LAYERS:
                    data = await fetch_tile(event_name, layer)
                    if data:
                        path = save_tile(year, event_name, session_type,
                                         now, layer, data)
                        logger.debug("Radar tile saved: %s (%d bytes)",
                                     path, len(data))
                await asyncio.sleep(REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weather radar capture loop crashed")
        finally:
            self._active_key = None
            self._stop_at = None


# Singleton — imported by main.py and the router.
radar_capture = WeatherRadarCapture()
