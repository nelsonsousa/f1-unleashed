"""
Weather Processor — pass through weather data as-is.

Subscribes to: WeatherData
Emits: display:weatherData
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class WeatherProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)

    def subscribe(self) -> None:
        self._bus.on("WeatherData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict):
            self._bus.emit("weatherData", data, clock_time)

    def snapshot(self) -> dict[str, Any]:
        return {}

    def restore(self, state: dict[str, Any]) -> None:
        pass

    def reset(self) -> None:
        pass
