"""
Standings Processor — classification ORDER (+ qualifying knockout state).

Subscribes to: TimingData, qualifyingPart (qualifying), SessionInfo (qualifying)
Emits:
  standings           { drivers: [ {num, position} ] }  ordered by Position
  qualifyingSegment   { segment, eliminated, isSprintQuali }   (qualifying only)

This is ordering only — every per-driver cell (lap times, gap, tyres, status,
penalties, lap count) is rendered by the tile from the focused per-driver
topics (driverLaps, driverGap, currentTyre, tyreHistory, driverStatus,
driverSectors, driverMiniSectors, fiaStewards). The tile renders one row per
driver in this order and fills the cells by car number.

Qualifying: `eliminated` is the cumulative set of knocked-out drivers (F1's
per-driver KnockedOut flag); `segment` is S?Q1/2/3. The per-driver elimination
ZONE (cutoff) is owned by the DriverGapProcessor (driverGap.cutoff).
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

# Fallback team colours (2026) when DriverList omits TeamColour. Kept here as
# the canonical table — imported by the DriverListProcessor.
TEAM_COLORS = {
    "1": "#ff8000", "81": "#ff8000",       # McLaren
    "3": "#1e3d7b", "6": "#1e3d7b",        # Red Bull
    "16": "#e8002d", "44": "#e8002d",      # Ferrari
    "12": "#00d4be", "63": "#00d4be",      # Mercedes
    "14": "#1a7a5a", "18": "#1a7a5a",      # Aston Martin
    "10": "#00a1e8", "43": "#00a1e8",      # Alpine
    "23": "#0f4c91", "55": "#0f4c91",      # Williams
    "30": "#2d826d", "41": "#2d826d",      # Racing Bulls
    "31": "#ffffff", "87": "#ffffff",      # Haas
    "5": "#990000", "27": "#990000",       # Audi
    "11": "#6e6e70", "77": "#6e6e70",      # Cadillac
}
DEFAULT_CAR_COLOR = "#888888"


class StandingsProcessor(Processor):
    """Classification order + qualifying knockout state."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_qualifying = session_type == "qualifying"
        self._pos: dict[str, int] = {}
        self._eliminated: set[str] = set()
        self._is_sprint_quali = False
        self._qualifying_part: Optional[int] = None
        self._last_order: list[dict] = []

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle_timing)
        if self._is_qualifying:
            self._bus.on("qualifyingPart", self._handle_qualifying_part)
            self._bus.on("SessionInfo", self._handle_session_info)

    def _handle_timing(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        order_changed = False
        elim_changed = False
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "Position" in d:
                try:
                    p = int(d["Position"])
                    if p != self._pos.get(num):
                        self._pos[num] = p
                        order_changed = True
                except (TypeError, ValueError):
                    pass
            # KnockedOut is REVERSIBLE — a reinstated driver (e.g. when the P16 car
            # loses its best lap to track limits) is un-knocked — so track both
            # directions rather than latching.
            if self._is_qualifying and "KnockedOut" in d:
                ko = bool(d["KnockedOut"])
                if ko and num not in self._eliminated:
                    self._eliminated.add(num); elim_changed = True
                elif not ko and num in self._eliminated:
                    self._eliminated.discard(num); elim_changed = True

        if order_changed:
            self._emit_standings(clock_time)
        if elim_changed:
            self._emit_qualifying_segment(clock_time)

    def _emit_standings(self, clock_time: datetime) -> None:
        drivers = [
            {"num": num, "position": p}
            for num, p in sorted(self._pos.items(), key=lambda kv: kv[1])
            if p < 99
        ]
        if drivers != self._last_order:
            self._last_order = drivers
            self._bus.emit("standings", {"drivers": drivers}, clock_time)

    # ── Qualifying knockout state ──
    def _handle_qualifying_part(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, int) and 1 <= data <= 3 and data != self._qualifying_part:
            self._qualifying_part = data
            self._emit_qualifying_segment(clock_time)

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        name = (data.get("Name") or "").lower()
        if "sprint qualifying" in name or "sprint shootout" in name:
            self._is_sprint_quali = True

    def _emit_qualifying_segment(self, clock_time: datetime) -> None:
        if self._qualifying_part is None:
            return
        prefix = "S" if self._is_sprint_quali else ""
        self._bus.emit("qualifyingSegment", {
            "segment": f"{prefix}Q{self._qualifying_part}",
            "eliminated": sorted(self._eliminated),
            "isSprintQuali": self._is_sprint_quali,
        }, clock_time)
