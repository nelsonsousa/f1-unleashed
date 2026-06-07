"""
Driver List Processor — driver identity and standings order.

Subscribes to: DriverList
Emits:
  - display:driverList  (first message: driver info keyed by car number)
  - display:standings    (ordered array of car numbers by position)
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class DriverListProcessor(Processor):
    """Extracts driver info and maintains standings order from DriverList."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._drivers: dict[str, dict] = {}   # num -> {tla, teamName, teamColour}
        self._standings: list[str] = []        # car numbers ordered by position
        self._initialized = False

    def subscribe(self) -> None:
        self._bus.on("DriverList", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return

        # F1 sends DriverList as an initial record followed by incremental
        # updates (each containing only the changed fields per driver).
        # We must MERGE every message — the first DriverList in a
        # downloaded archive may carry only Tla/Line, with TeamName /
        # TeamColour arriving in a later incremental update (canonical
        # case observed for Canada FP1 2026).
        info_changed = False    # tla / teamName / teamColour changed
        order_changed = False   # Line (standings order) changed
        for num, info in data.items():
            if not isinstance(info, dict):
                continue
            existing = self._drivers.get(num)
            if existing is None:
                existing = {"tla": "", "teamName": "", "teamColour": "", "lastName": ""}
                self._drivers[num] = existing
                info_changed = True
            for src, dst in (("Tla", "tla"),
                             ("TeamName", "teamName"),
                             ("TeamColour", "teamColour"),
                             ("LastName", "lastName")):
                v = info.get(src)
                if v and existing.get(dst) != v:
                    existing[dst] = v
                    info_changed = True
            line = info.get("Line")
            if line is not None:
                self._set_position(num, int(line))
                order_changed = True

        if not self._initialized:
            self._initialized = True
            info_changed = True   # always emit on first message

        if info_changed:
            self._bus.emit("driverList", dict(self._drivers), clock_time)
        if order_changed:
            self._bus.emit("standings", [n for n in self._standings if n], clock_time)

    def _set_position(self, num: str, line: int) -> None:
        """Place driver at the given 1-based position in standings."""
        # Remove from current position if present
        if num in self._standings:
            self._standings.remove(num)

        # Extend array if needed
        idx = line - 1
        while len(self._standings) < idx:
            self._standings.append("")
        self._standings.insert(idx, num)

    def snapshot(self) -> dict[str, Any]:
        return {
            "drivers": dict(self._drivers),
            "standings": list(self._standings),
            "initialized": self._initialized,
        }

    def restore(self, state: dict[str, Any]) -> None:
        self._drivers = state.get("drivers", {})
        self._standings = state.get("standings", [])
        self._initialized = state.get("initialized", False)

    def reset(self) -> None:
        self._drivers.clear()
        self._standings.clear()
        self._initialized = False
