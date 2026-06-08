"""
Clock Processor — track time, session time, clock status.

Subscribes to: ExtrapolatedClock, SessionInfo
Emits: display:clock

trackTime: UTC adjusted by GMT offset (local track time)
sessionTime: the Remaining field from ExtrapolatedClock
clockStatus: "play" when Extrapolating=true, "pause" when false
"""

from datetime import datetime, timedelta
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_gmt_offset(offset_str: str) -> Optional[timedelta]:
    """Parse GMT offset like '11:00:00' or '-05:00:00' to timedelta."""
    if not offset_str:
        return None
    try:
        negative = offset_str.startswith("-")
        parts = offset_str.lstrip("-").split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        td = timedelta(hours=hours, minutes=minutes)
        return -td if negative else td
    except (ValueError, IndexError):
        return None


class ClockProcessor(Processor):
    """Produces track time, session time, and clock status."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._gmt_offset: Optional[timedelta] = None
        self._utc: Optional[str] = None
        self._remaining: Optional[str] = None
        self._clock_status: str = "pause"

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle_session_info)
        self._bus.on("ExtrapolatedClock", self._handle_clock)

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        gmt = data.get("GmtOffset")
        if gmt:
            self._gmt_offset = _parse_gmt_offset(gmt)

    def _handle_clock(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        if "Utc" in data:
            self._utc = data["Utc"]
        if "Remaining" in data:
            self._remaining = data["Remaining"]
        if "Extrapolating" in data:
            self._clock_status = "play" if data["Extrapolating"] else "pause"

        self._bus.emit("clock", {
            "utc": self._utc,
            "sessionTime": self._remaining,
            "clockStatus": self._clock_status,
        }, clock_time)
