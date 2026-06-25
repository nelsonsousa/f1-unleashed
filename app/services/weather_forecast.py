"""Weather forecast capture (card 118).

During a live session, fetch the Open-Meteo `minutely_15` forecast (weather_code +
precipitation_probability) for the circuit every 10 min and append a snapshot to
`{session_dir}/weather_forecast.jsonl`. Replay reads these (served by
`/api/v1/weather/forecast`) and indexes them by the playback clock — Open-Meteo
does NOT archive past forecasts, so capturing live is the only way to replay what
was predicted. The first entry of each snapshot (at capture time) doubles as the
current condition.

Mirrors the radar-capture lifecycle (start/stop/schedule_stop), triggered from the
live-session monitor alongside radar_capture.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from app.services.weather_radar import TRACK_LOCATIONS

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
REFRESH_INTERVAL_S = 600          # 10 min, like the weather tiles
FORECAST_FILE = "weather_forecast.jsonl"


async def fetch_forecast(meeting_name: str) -> Optional[dict]:
    """Open-Meteo 15-minute forecast for the circuit (next ~2 h)."""
    coord = TRACK_LOCATIONS.get(meeting_name)
    if not coord:
        logger.warning("forecast: no circuit coords for %r", meeting_name)
        return None
    lat, lng = coord
    params = {
        "latitude": lat, "longitude": lng,
        "minutely_15": "weather_code,precipitation_probability",
        "forecast_minutely_15": 8,        # 8 × 15 min = 2 h ahead
        "timezone": "UTC",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(FORECAST_URL, params=params) as r:
                if r.status != 200:
                    logger.warning("forecast HTTP %s", r.status)
                    return None
                raw = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("forecast fetch failed: %s", e)
        return None
    m = raw.get("minutely_15", {})
    if not m.get("time"):
        return None
    return {
        "time": m.get("time", []),
        "weather_code": m.get("weather_code", []),
        "precipitation_probability": m.get("precipitation_probability", []),
    }


def append_snapshot(session_dir: Path, captured_utc: datetime, forecast: dict) -> None:
    rec = {"captured": captured_utc.replace(microsecond=0).isoformat() + "Z", **forecast}
    try:
        with open(Path(session_dir) / FORECAST_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        logger.error("forecast snapshot write failed: %s", e)


class ForecastCapture:
    """Singleton periodic forecast capture for the active live session."""

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._active_key: Optional[str] = None
        self._session_dir: Optional[Path] = None
        self._meeting_name: Optional[str] = None
        self._stop_at: Optional[datetime] = None

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
            if stop_at is not None:
                self._stop_at = stop_at
            return
        if self.active:
            self._cancel()
        self._active_key = key
        self._session_dir = Path(session_dir)
        self._meeting_name = meeting_name
        self._stop_at = stop_at
        self._task = asyncio.create_task(self._loop())
        logger.info("Forecast capture started for %s -> %s", meeting_name, key)

    def stop(self) -> None:
        if not self.active:
            return
        self._cancel()
        self._active_key = None
        self._session_dir = None
        self._meeting_name = None
        self._stop_at = None

    def schedule_stop(self, when: datetime) -> None:
        if self.active:
            self._stop_at = when

    def _cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                if self._stop_at and datetime.now(timezone.utc) >= self._stop_at:
                    break
                fc = await fetch_forecast(self._meeting_name)
                if fc:
                    append_snapshot(self._session_dir, datetime.now(timezone.utc), fc)
                await asyncio.sleep(REFRESH_INTERVAL_S)
        except asyncio.CancelledError:
            raise


forecast_capture = ForecastCapture()
