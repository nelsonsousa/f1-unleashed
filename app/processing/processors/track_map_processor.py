"""
Track Map Processor — car positions for track map rendering.

Subscribes to: Position.z
Emits: display:position

Emits a compact message with all car coordinates on each Position.z update.
Format: {"1": [x, y], "44": [x, y], ...}

Flags, pit/retired status, and driver info are handled by other processors
(track-status, race-control, standings).
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class TrackMapProcessor(Processor):
    """Emits car positions as a compact coordinate map."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._positions: dict[str, list[float]] = {}

    def subscribe(self) -> None:
        self._bus.on("Position.z", self._handle_position)

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        pos_data = data.get("Position") or data
        if not isinstance(pos_data, list) or not pos_data:
            return

        latest = pos_data[-1]
        entries = latest.get("Entries") or latest
        if not isinstance(entries, dict):
            return

        for num, pos in entries.items():
            if not isinstance(pos, dict):
                continue
            x = pos.get("X")
            y = pos.get("Y")
            if x is None or y is None:
                continue
            if x == 0 and y == 0:
                continue
            self._positions[num] = [round(x, 1), round(y, 1)]

        if self._positions:
            self._bus.emit("position", dict(self._positions), clock_time)
