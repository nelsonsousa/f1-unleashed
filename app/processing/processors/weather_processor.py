"""
Weather Processor — track/air conditions.

Subscribes to: WeatherData
Emits: weatherData  { airTemp, trackTemp, pressure, humidity, rain,
                      windSpeed, windDirection }   (persisted)

All fields are sticky F1 deltas — each is updated only when present and carried
forward otherwise.
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

_FIELDS = {
    "AirTemp": "airTemp",
    "TrackTemp": "trackTemp",
    "Pressure": "pressure",
    "Humidity": "humidity",
    "Rainfall": "rain",
    "WindSpeed": "windSpeed",
    "WindDirection": "windDirection",
}


def _num(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


class WeatherProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._state: dict[str, Any] = {}

    def subscribe(self) -> None:
        self._bus.on("WeatherData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        changed = False
        for src, dst in _FIELDS.items():
            if src in data:
                v = _num(data[src])
                if self._state.get(dst) != v:
                    self._state[dst] = v
                    changed = True
        if changed:
            self._bus.emit("weatherData", dict(self._state), clock_time)
