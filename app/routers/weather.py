import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
import httpx

from app.services.weather_radar import (
    LAYERS, TRACK_LOCATIONS, composite_geometry, latest_cached_tile,
    radar_capture, usage_status,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory cache: {(lat, lng, date_str): {"data": ..., "fetched_at": datetime}}
_cache: dict = {}
CACHE_TTL_HOURS = 6

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_PARAMS = "temperature_2m,windspeed_10m,winddirection_10m,cloudcover,precipitation,weather_code,is_day"


@router.get("/weather")
async def get_weather(
    latitude: float = Query(...),
    longitude: float = Query(...),
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
):
    """Fetch hourly weather data for a circuit on a given date."""
    # Validate date format
    try:
        target_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    # Round coordinates for cache key
    cache_key = (round(latitude, 2), round(longitude, 2), date)

    # Check cache
    if cache_key in _cache:
        entry = _cache[cache_key]
        if datetime.now() - entry["fetched_at"] < timedelta(hours=CACHE_TTL_HOURS):
            return entry["data"]

    # Determine which API to use
    today = datetime.now().date()
    days_ago = (today - target_date).days

    if days_ago <= 5:
        url = FORECAST_URL
    else:
        url = ARCHIVE_URL

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": date,
        "end_date": date,
        "hourly": HOURLY_PARAMS,
        "timezone": "UTC",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            raw = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Open-Meteo API error: {e.response.status_code} - {e.response.text}")
        raise HTTPException(status_code=502, detail="Weather API returned an error")
    except httpx.RequestError as e:
        logger.error(f"Open-Meteo request failed: {e}")
        raise HTTPException(status_code=502, detail="Failed to reach weather API")

    hourly = raw.get("hourly", {})

    result = {
        "date": date,
        "latitude": latitude,
        "longitude": longitude,
        "hourly": {
            "time": hourly.get("time", []),
            "temperature_2m": hourly.get("temperature_2m", []),
            "windspeed_10m": hourly.get("windspeed_10m", []),
            "winddirection_10m": hourly.get("winddirection_10m", []),
            "cloudcover": hourly.get("cloudcover", []),
            "precipitation": hourly.get("precipitation", []),
            "weather_code": hourly.get("weather_code", []),
            "is_day": hourly.get("is_day", []),
        },
    }

    # Store in cache
    _cache[cache_key] = {"data": result, "fetched_at": datetime.now()}

    return result


# ─────────────────────────── Weather radar ───────────────────────────

@router.get("/weather/radar/latest")
def radar_latest(
    session: str = Query(..., description="Session cache key, e.g. 2026_1287_Barcelona_11307_Race"),
    layer: str = Query("precipitationIntensity",
                        description="Radar layer; only precipitationIntensity"),
    t: Optional[int] = Query(
        None,
        description="If set, returns the tile closest to this UTC ms timestamp "
                    "(used by replays so the rain matches the playback clock); "
                    "if omitted, returns the latest cached tile.",
    ),
):
    """Return the radar tile (PNG) for the session+layer. Tiles live in the
    session's own cache dir (card). With `t`, picks the cached tile closest in
    time to that moment (replay use); without `t`, the most recent cached tile
    (live use). 204 if no tiles are cached yet."""
    if layer not in LAYERS:
        raise HTTPException(status_code=400, detail=f"Unknown layer; use one of {LAYERS}")
    from app.services.livetiming_fetcher import livetiming_fetcher
    session_dir = livetiming_fetcher.find_cached_session_path(session)
    if session_dir is None:
        return Response(status_code=204)
    if t is not None:
        from app.services.weather_radar import cached_tile_at
        target = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        path = cached_tile_at(session_dir, layer, target)
    else:
        path = latest_cached_tile(session_dir, layer)
    if path is None:
        return Response(status_code=204)
    return FileResponse(
        path, media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Tile-Id": path.stem},
    )


@router.get("/weather/forecast")
def weather_forecast(
    session: str = Query(..., description="Session cache key, e.g. 2026_1287_Barcelona_11307_Race"),
):
    """Captured 15-min forecast snapshots for the session (card 118). Each snapshot:
    {captured, time[], weather_code[], precipitation_probability[]}; the client
    indexes them by the playback clock. 204 if nothing was captured."""
    import json as _json
    from app.services.livetiming_fetcher import livetiming_fetcher
    from app.services.weather_forecast import FORECAST_FILE
    session_dir = livetiming_fetcher.find_cached_session_path(session)
    if session_dir is None:
        return Response(status_code=204)
    fp = session_dir / FORECAST_FILE
    if not fp.exists():
        return Response(status_code=204)
    snaps = []
    try:
        for line in fp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                snaps.append(_json.loads(line))
    except (OSError, ValueError):
        return Response(status_code=204)
    if not snaps:
        return Response(status_code=204)
    return {"snapshots": snaps}


@router.get("/weather/radar/status")
def radar_status():
    """Diagnostic: is radar capture running, and for which session dir?"""
    return {
        "active": radar_capture.active,
        "session_dir": radar_capture.active_key,
    }


@router.get("/weather/radar/usage")
def radar_usage():
    """Rainbow.ai monthly call usage + remaining budget. Lets a client check
    before a session whether the radar can run (Rainbow has no usage endpoint
    of its own, so this is our local counter)."""
    return usage_status()


@router.get("/weather/radar/extent")
def radar_extent(event_name: str = Query(...)):
    """Geographic context for a circuit's radar overlay so the frontend can
    size AND position it over the track. The overlay is a centred 2x2 zoom-14
    composite; we return its square physical extent (metres) and the circuit's
    fractional position within it so the frontend can offset the tile to land
    the rain on the track centre. The tile is north-up; the frontend rotates
    it to match the track's own (baked-in) `data-rotation`."""
    loc = TRACK_LOCATIONS.get(event_name)
    if not loc:
        raise HTTPException(status_code=404, detail=f"Unknown circuit: {event_name!r}")
    lat, lng = loc
    geo = composite_geometry(lat, lng)
    return {
        "lat": lat,
        "lng": lng,
        "tile_zoom": geo["zoom"],
        "composite_tiles": geo["tiles"],
        "tile_width_m": geo["width_m"],
        "tile_height_m": geo["height_m"],
        "circuit_frac_x": geo["circuit_frac_x"],
        "circuit_frac_y": geo["circuit_frac_y"],
    }
