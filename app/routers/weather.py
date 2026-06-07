import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response
import httpx

from app.services.weather_radar import (
    LAYERS, TILE_ZOOM, TRACK_LOCATIONS, latest_cached_tile,
    list_cached_tiles, radar_capture, tile_size_m,
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
    year: int = Query(...),
    event_name: str = Query(..., description="Meeting name, e.g. 'Australian Grand Prix'"),
    session_type: str = Query(...),
    layer: str = Query("precipitationIntensity",
                        description="Radar layer; only precipitationIntensity"),
    t: Optional[int] = Query(
        None,
        description="If set, returns the tile closest to this UTC ms timestamp "
                    "(used by replays so the rain matches the playback clock); "
                    "if omitted, returns the latest cached tile.",
    ),
):
    """Return the radar tile (PNG) for the given session+layer. With
    `t`, picks the cached tile closest in time to that moment (replay
    use). Without `t`, returns the most recent cached tile (live use).
    204 if no tiles are cached yet."""
    if layer not in LAYERS:
        raise HTTPException(status_code=400, detail=f"Unknown layer; use one of {LAYERS}")
    if t is not None:
        from app.services.weather_radar import cached_tile_at
        target = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        path = cached_tile_at(year, event_name, session_type, layer, target)
    else:
        path = latest_cached_tile(year, event_name, session_type, layer)
    if path is None:
        return Response(status_code=204)
    return FileResponse(
        path, media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/weather/radar/status")
def radar_status():
    """Diagnostic: is radar capture running, and for which session?"""
    key = radar_capture.active_key
    return {
        "active": radar_capture.active,
        "year": key[0] if key else None,
        "event_name": key[1] if key else None,
        "session_type": key[2] if key else None,
    }


@router.get("/weather/radar/extent")
def radar_extent(event_name: str = Query(...)):
    """Return the geographic context for a circuit's radar tile so the
    frontend can scale the track SVG to the same metre-per-pixel scale
    as the tile. The tile is square in pixel space; physical extent is
    `40_075_017 / 2^zoom · cos(lat)` metres at the tile's latitude."""
    loc = TRACK_LOCATIONS.get(event_name)
    if not loc:
        raise HTTPException(status_code=404, detail=f"Unknown circuit: {event_name!r}")
    lat, lng = loc
    tile_m = tile_size_m(lat, TILE_ZOOM)
    return {
        "lat": lat,
        "lng": lng,
        "tile_zoom": TILE_ZOOM,
        "tile_width_m": tile_m,
        "tile_height_m": tile_m,
    }
