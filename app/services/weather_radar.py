"""Rainbow.ai weather-radar capture and cache.

Builds one precipitation overlay per refresh for the F1 circuit in the active
session, on a schedule synchronised to the session window:

    session_start − 15 min   →   start capture
    session_end   + 5 min    →   stop capture
    refresh interval         =   10 min  (Rainbow updates the nowcast every 10 min)

Each refresh fetches a centred 2x2 zoom-12 block (Rainbow's max zoom) of the
raw-reflectivity ``dbz_u8`` tiles, stitches them to 512x512, and renders them
through a light→dark blue palette (transparent below the rain threshold). The
result is saved as one RGBA PNG in the session's own cache dir and served by
``/api/v1/weather/radar/latest``. Live captures cache once; replays read from
disk and never re-fetch.

Free tier: 30,000 calls/month, no hourly cap. Each refresh = 1 snapshot + 4
tiles = 5 calls (~30/hr during a session). A persistent monthly counter
(`MONTHLY_CALL_BUDGET`, see ``rainbow_usage.json``) stops calls before the cap.

Get a subscription key at https://developer.rainbow.ai/ and set
``RAINBOW_AI_API_KEY`` in ``.env``.
"""

import asyncio
import io
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
from PIL import Image

from app import settings
from app.config import DATA_DIR

logger = logging.getLogger(__name__)

# Weather-radar tile cache — moved out of the repo to the OS-appropriate data
# dir alongside the livetiming cache (card 25).
CACHE_DIR = DATA_DIR / "weather_radar_cache"
# Rainbow refreshes the nowcast every 10 min, so fetching faster just wastes
# calls. One refresh = a centred 2x2 z12 composite. The overlay is rain only
# — overall sky condition is conveyed by the weather-condition icon (driven by
# /api/v1/weather), not a cloud-cover overlay.
REFRESH_INTERVAL_S = 600
LAYERS = ("precipitationIntensity",)   # canonical overlay name (cache + frontend)
TILE_EXT = "png"

# ── Rainbow.ai ─────────────────────────────────────────────────────────────
RAINBOW_BASE = "https://api.rainbow.ai/tiles/v1"
RAINBOW_KEY_ENV = "RAINBOW_AI_API_KEY"
# Free tier: 30k calls/month, no hourly cap. Stop short of the cap for safety.
MONTHLY_CALL_BUDGET = 29_000
USAGE_FILE = DATA_DIR / "rainbow_usage.json"

# Raw reflectivity (dbz_u8): red channel low 7 bits = dBZ + 32, top bit = snow.
# Rainbow's own palette draws rain at dBZ ≥ 7, so we match that cutoff.
DBZ_RAIN_MIN = 7
# dBZ → RGBA light→dark-blue ramp (np.interp clamps outside the stops). Light
# blue (semi-transparent) for drizzle, deep navy (opaque) for heavy rain.
BLUE_RAMP_DBZ = (7,   20,  35,  50,  65)
BLUE_RAMP_R = (214, 122, 40,  10,  3)
BLUE_RAMP_G = (234, 184, 110, 58,  28)
BLUE_RAMP_B = (255, 245, 210, 140, 85)
BLUE_RAMP_A = (140, 200, 235, 255, 255)

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


# Radar tiles are served as a centred 2x2 zoom-14 composite (512x512). A 2x2
# z14 block covers the same ground as one z13 tile but at double resolution,
# and — crucially — is positioned so the circuit sits near the composite
# centre rather than in an arbitrary slippy-grid corner, so the overlay can
# be pinned to the track.
COMPOSITE_ZOOM = 12        # Rainbow's max zoom
COMPOSITE_N = 2            # 2x2 tiles -> 512x512 px
COMPOSITE_PX = 256 * COMPOSITE_N


def _composite_block(lat: float, lng: float):
    """Fractional tile coords (xf, yf) of the circuit at COMPOSITE_ZOOM and the
    NW tile (x0, y0) of the 2x2 block chosen to centre it. Shared by the
    capture (which tiles to fetch) and the geometry (where the circuit sits)."""
    n = 2 ** COMPOSITE_ZOOM
    xf = (lng + 180.0) / 360.0 * n
    yf = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return xf, yf, round(xf) - 1, round(yf) - 1


def composite_geometry(lat: float, lng: float) -> dict:
    """Geometry of the centred 2x2 zoom-12 radar composite for a circuit.

    Returns the composite's physical extent (square, metres) and the circuit's
    fractional position within it (0..1 from the NW corner) so the frontend can
    size AND offset the overlay to land the rain on the track centre."""
    xf, yf, x0, y0 = _composite_block(lat, lng)
    span_m = tile_size_m(lat, COMPOSITE_ZOOM) * COMPOSITE_N
    return {
        "zoom": COMPOSITE_ZOOM,
        "tiles": COMPOSITE_N,
        "width_m": span_m,
        "height_m": span_m,
        "circuit_frac_x": (xf - x0) / COMPOSITE_N,
        "circuit_frac_y": (yf - y0) / COMPOSITE_N,
    }


def _weather_dir(session_dir) -> Path:
    """Weather tiles live inside the session's OWN cache dir (card), next to
    live.jsonl / commentary.aac — so weather, timing and audio share one path
    keyed by the F1 meeting/session ids."""
    return Path(session_dir) / "weather"


def list_cached_tiles(session_dir, layer: str) -> list[Path]:
    """Return chronologically-sorted PNG files for a session+layer."""
    d = _weather_dir(session_dir)
    if not d.exists():
        return []
    return sorted(d.glob(f"*_{layer}.{TILE_EXT}"))


def has_cached_weather(session_dir) -> bool:
    """True if ANY weather-radar tile is cached for the session (any layer).
    Presence check for the home-page weather icon (card)."""
    d = _weather_dir(session_dir)
    return d.exists() and any(d.glob(f"*.{TILE_EXT}"))


def latest_cached_tile(session_dir, layer: str) -> Optional[Path]:
    tiles = list_cached_tiles(session_dir, layer)
    return tiles[-1] if tiles else None


def cached_tile_at(session_dir, layer: str, target_utc: datetime) -> Optional[Path]:
    """Return the most recent cached tile AT OR BEFORE `target_utc` (causal —
    never shows rain captured after the playback clock). Filenames are
    `YYYYMMDDTHHMMSSZ_<layer>.png`. Returns None when every cached tile is
    later than the target (e.g. seeking before the first tile) so the caller
    shows no overlay."""
    tiles = list_cached_tiles(session_dir, layer)
    best, best_ts = None, None
    for t in tiles:
        stem = t.stem.split("_", 1)[0]  # "20260524T194519Z"
        try:
            ts = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts <= target_utc and (best_ts is None or ts > best_ts):
            best, best_ts = t, ts
    return best


# ── Monthly call counter ───────────────────────────────────────────────────
# Rainbow exposes no usage endpoint or quota headers, so we track calls
# ourselves in a small JSON file and stop before the monthly cap.
_usage_lock = threading.Lock()


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _usage_load() -> dict:
    try:
        d = json.loads(USAGE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    if d.get("month") != _current_month():     # rollover (or first run) → reset
        d = {"month": _current_month(), "count": 0}
    return d


def usage_status() -> dict:
    """Current month's Rainbow call count + remaining budget. Safe to call
    before a session to decide whether the radar can run."""
    with _usage_lock:
        d = _usage_load()
    return {
        "month": d["month"],
        "count": d["count"],
        "limit": MONTHLY_CALL_BUDGET,
        "remaining": max(0, MONTHLY_CALL_BUDGET - d["count"]),
    }


def _usage_add(n: int) -> None:
    with _usage_lock:
        d = _usage_load()
        d["count"] += n
        try:
            USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
            USAGE_FILE.write_text(json.dumps(d))
        except OSError:
            logger.warning("Could not persist Rainbow usage counter")


def _rainbow_key() -> Optional[str]:
    return settings.get("rainbowAiApiKey") or None


def _render_blue(red: np.ndarray) -> bytes:
    """Map a HxW dbz_u8 red-channel array to an RGBA blue-palette PNG.
    Pixels below the rain threshold are fully transparent."""
    dbz = (red & 0x7F).astype(np.int16) - 32          # low 7 bits = dBZ + 32
    r = np.interp(dbz, BLUE_RAMP_DBZ, BLUE_RAMP_R)
    g = np.interp(dbz, BLUE_RAMP_DBZ, BLUE_RAMP_G)
    b = np.interp(dbz, BLUE_RAMP_DBZ, BLUE_RAMP_B)
    a = np.interp(dbz, BLUE_RAMP_DBZ, BLUE_RAMP_A)
    a = np.where(dbz >= DBZ_RAIN_MIN, a, 0)           # clear sky → transparent
    rgba = np.dstack([r, g, b, a]).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


async def fetch_composite(meeting_name: str) -> Optional[tuple[np.ndarray, dict]]:
    """Fetch the centred 2x2 zoom-12 ``dbz_u8`` block for a circuit and return
    ``(red, geometry)`` — the raw 512x512 reflectivity red-channel grid and the
    composite geometry (for pinning + the rain alert). None on missing key /
    unknown circuit / monthly budget reached / upstream failure. Each call uses
    5 API calls (1 snapshot + 4 tiles)."""
    key = _rainbow_key()
    if not key:
        logger.warning("%s not set; skipping radar fetch", RAINBOW_KEY_ENV)
        return None
    loc = TRACK_LOCATIONS.get(meeting_name)
    if not loc:
        logger.warning("No radar coords for meeting %r; skipping", meeting_name)
        return None
    if usage_status()["remaining"] < 5:
        logger.warning("Rainbow monthly call budget reached (%s); skipping radar",
                       usage_status())
        return None

    lat, lng = loc
    xf, yf, x0, y0 = _composite_block(lat, lng)
    headers = {"Ocp-Apim-Subscription-Key": key}
    red = np.zeros((COMPOSITE_PX, COMPOSITE_PX), dtype=np.uint8)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            rs = await client.get(f"{RAINBOW_BASE}/snapshot", headers=headers)
            rs.raise_for_status()
            _usage_add(1)
            snapshot = int(rs.json()["snapshot"])
            for dy in range(COMPOSITE_N):
                for dx in range(COMPOSITE_N):
                    url = (f"{RAINBOW_BASE}/precip/{snapshot}/0/"
                           f"{COMPOSITE_ZOOM}/{x0 + dx}/{y0 + dy}")
                    rt = await client.get(url, params={"color": "dbz_u8"},
                                          headers=headers)
                    rt.raise_for_status()
                    _usage_add(1)
                    tile = Image.open(io.BytesIO(rt.content)).convert("RGBA")
                    arr = np.asarray(tile)[:, :, 0]
                    red[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = arr
    except httpx.HTTPStatusError as e:
        logger.warning("Rainbow fetch failed (%s): %s",
                       e.response.status_code, e.response.text[:200])
        return None
    except httpx.RequestError as e:
        logger.warning("Rainbow request error: %s", e)
        return None
    return red, composite_geometry(lat, lng)


def raw_dbz_png(red: np.ndarray) -> bytes:
    """Encode the raw dbz_u8 red-channel composite as a lossless grayscale PNG.
    This is the ORIGINAL reflectivity (not a recoloured render) — the SVG contour
    JSON is derived from it, and it stays the durable source so the bands can be
    re-derived later. Reconstruct dBZ with ``(pixel & 0x7F) - 32``."""
    buf = io.BytesIO()
    Image.fromarray(red.astype(np.uint8), "L").save(buf, format="PNG")
    return buf.getvalue()


def save_tile(session_dir, fetched_at: datetime, layer: str, data: bytes) -> Path:
    d = _weather_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    iso = fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = d / f"{iso}_{layer}.{TILE_EXT}"
    out.write_bytes(data)
    return out


def save_contours(session_dir, fetched_at: datetime, layer: str, payload: dict) -> Path:
    """Persist the per-snapshot SVG-contour JSON next to its raw tile. Created
    once at capture; the durable source for replay if the PNG is later deleted."""
    d = _weather_dir(session_dir)
    d.mkdir(parents=True, exist_ok=True)
    iso = fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = d / f"{iso}_{layer}.json"
    out.write_text(json.dumps(payload))
    return out


def cached_contours_at(session_dir, layer: str,
                       target_utc: datetime) -> Optional[Path]:
    """Most-recent contour JSON at/before ``target_utc`` (causal). Mirrors
    ``cached_tile_at`` but for the ``_{layer}.json`` sidecars."""
    d = _weather_dir(session_dir)
    if not d.exists():
        return None
    best, best_ts = None, None
    for f in d.glob(f"*_{layer}.json"):
        stem = f.stem.split("_", 1)[0]
        try:
            ts = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts <= target_utc and (best_ts is None or ts > best_ts):
            best, best_ts = f, ts
    return best


def latest_contours(session_dir, layer: str) -> Optional[Path]:
    d = _weather_dir(session_dir)
    if not d.exists():
        return None
    files = sorted(d.glob(f"*_{layer}.json"))
    return files[-1] if files else None


def backfill_contours(session_dir, meeting_name: str,
                      layer: str = LAYERS[0]) -> int:
    """Generate the contour JSON for any cached RAW-dbz tile lacking a sidecar
    (sessions captured before contours existed can be re-derived). Requires the
    tile PNGs to be raw dbz (grayscale). Returns the count written."""
    from app.services import weather_contours as wc
    loc = TRACK_LOCATIONS.get(meeting_name)
    if not loc:
        logger.warning("backfill_contours: no coords for %r", meeting_name)
        return 0
    geo = composite_geometry(*loc)
    n = 0
    for png in list_cached_tiles(session_dir, layer):
        js = png.with_suffix(".json")
        if js.exists():
            continue
        red = np.asarray(Image.open(png).convert("L"))
        js.write_text(json.dumps(wc.build_contour_json(red, geo)))
        n += 1
    return n


class WeatherRadarCapture:
    """Drives the periodic tile fetch for one active session. Idempotent
    start/stop; only one session can be active at a time."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._active_key: Optional[str] = None       # the session cache dir (str)
        self._session_dir: Optional[Path] = None
        self._meeting_name: Optional[str] = None
        self._stop_at: Optional[datetime] = None  # if set, loop exits after this

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def active_key(self) -> Optional[str]:
        return self._active_key

    def start(self, session_dir, meeting_name: str,
              stop_at: Optional[datetime] = None) -> None:
        key = str(session_dir)
        if self.active and self._active_key == key:
            # Already running for the same session — update stop_at if given.
            if stop_at is not None:
                self._stop_at = stop_at
            return
        if self.active:
            logger.info("Switching radar capture to %s (was %s)",
                        key, self._active_key)
            self._cancel()
        self._active_key = key
        self._session_dir = Path(session_dir)
        self._meeting_name = meeting_name
        self._stop_at = stop_at
        self._task = asyncio.create_task(self._loop())
        logger.info("Radar capture started for %s -> %s%s",
                    meeting_name, key,
                    f" (stop at {stop_at.isoformat()})" if stop_at else "")

    def stop(self) -> None:
        if not self.active:
            return
        logger.info("Radar capture stopping for %s", self._active_key)
        self._cancel()
        self._active_key = None
        self._session_dir = None
        self._meeting_name = None
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
                now = datetime.now(timezone.utc)
                result = await fetch_composite(self._meeting_name)
                if result:
                    from app.services import weather_contours as wc
                    red, geo = result
                    save_tile(self._session_dir, now, LAYERS[0], raw_dbz_png(red))
                    save_contours(self._session_dir, now, LAYERS[0],
                                  wc.build_contour_json(red, geo))
                    logger.debug("Radar raw tile + contours saved for %s", now)
                await asyncio.sleep(REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weather radar capture loop crashed")
        finally:
            self._active_key = None
            self._session_dir = None
            self._meeting_name = None
            self._stop_at = None


# Singleton — imported by main.py and the router.
radar_capture = WeatherRadarCapture()
