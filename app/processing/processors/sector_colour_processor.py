"""
Sector Colour Processor — per-sector colour for the S1/S2/S3 cells.

Subscribes (wildcard) to: driverSectors:{num}, driverLaps:{num},
                          driverStatus:{num}, standings, qualifyingPart
Emits: driverSectorColour:{num}  [c0, c1, c2]   (per-sector colour class or null)

Reference:
  practice/qualifying : best overall sector time — per sector, min across drivers;
                        reset each Q part.
  race                : the LEADER's sector time in the SAME lap (lap N vs lap N).
Bands (Δ = driver_sector − reference):
    <= 0    purple   (fastest overall / at-or-faster-than the leader's sector)
    < 0.1   blue
    < 0.2   green
    < 0.5   yellow
    < 1.0   orange
    >= 1.0  red
In/out/stop laps → white (driverStatus OUT/PIT/STOP — the current-lap signal, which
the per-lap classification can't give until the lap completes).
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

WHITE_STATUS = {"OUT", "PIT", "STOP"}


def _parse_sector_ms(v: Any) -> Optional[int]:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return round(float(v) * 1000)   # sector strings are seconds, e.g. "25.108"
    except ValueError:
        return None


class SectorColourProcessor(Processor):
    """Per-sector colour vs best-overall (P/Q) or the leader's same-lap (race)."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._sectors: dict[str, list] = {}          # num -> [ms|None x3]
        self._lap: dict[str, int] = {}               # num -> current lap
        self._status: dict[str, Optional[str]] = {}
        self._best_sec: list = [None, None, None]     # P/Q best overall per sector
        self._leader: Optional[str] = None
        self._leader_sec: dict[int, list] = {}        # race: lap -> [ms|None x3]
        self._part: Optional[int] = None
        self._emitted: dict[str, tuple] = {}          # dedup

    def subscribe(self) -> None:
        self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverSectors:"):
            self._on_sectors(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data)
        elif topic.startswith("driverStatus:"):
            self._on_status(topic.split(":", 1)[1], data, clock_time)
        elif topic == "standings":
            self._on_standings(data, clock_time)
        elif topic == "qualifyingPart":
            self._on_part(data, clock_time)

    def _on_laps(self, num: str, data: Any) -> None:
        if isinstance(data, dict) and data.get("currentLap") is not None:
            self._lap[num] = data["currentLap"]

    def _on_status(self, num: str, data: Any, clock_time: datetime) -> None:
        st = data if isinstance(data, str) else None
        if st != self._status.get(num):
            self._status[num] = st
            self._emit(num, clock_time)               # white toggle

    def _on_part(self, data: Any, clock_time: datetime) -> None:
        part = data if isinstance(data, int) else None
        if part == self._part:
            return
        self._part = part
        self._best_sec = [None, None, None]           # best sectors reset per part
        for num in list(self._sectors):
            self._emit(num, clock_time)

    def _on_standings(self, data: Any, clock_time: datetime) -> None:
        drivers = data.get("drivers") if isinstance(data, dict) else None
        leader = drivers[0]["num"] if drivers else None
        if leader != self._leader:
            self._leader = leader
            if self._is_race:
                for num in list(self._sectors):
                    self._emit(num, clock_time)

    def _on_sectors(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, list):
            return
        vals = [None, None, None]
        for i in range(min(3, len(data))):
            s = data[i]
            vals[i] = _parse_sector_ms(s.get("value") if isinstance(s, dict) else None)
        self._sectors[num] = vals
        recompute_all = False
        if self._is_race:
            if num == self._leader:
                lap = self._lap.get(num)
                if lap is not None:
                    ls = self._leader_sec.setdefault(lap, [None, None, None])
                    for i in range(3):
                        if vals[i] is not None:
                            ls[i] = vals[i]
                    recompute_all = True              # leader reference moved
        else:
            for i in range(3):                        # P/Q best overall per sector
                if vals[i] is not None and (self._best_sec[i] is None or vals[i] < self._best_sec[i]):
                    self._best_sec[i] = vals[i]
                    recompute_all = True
        if recompute_all:
            for n in list(self._sectors):
                self._emit(n, clock_time)
        else:
            self._emit(num, clock_time)

    def _band(self, delta_ms: int) -> str:
        if delta_ms <= 0:
            return "purple"
        s = delta_ms / 1000.0
        if s < 0.1:
            return "blue"
        if s < 0.2:
            return "green"
        if s < 0.5:
            return "yellow"
        if s < 1.0:
            return "orange"
        return "red"

    def _colours_for(self, num: str) -> list:
        vals = self._sectors.get(num)
        if not vals:
            return [None, None, None]
        if self._status.get(num) in WHITE_STATUS:
            return ["white" if v is not None else None for v in vals]
        out = [None, None, None]
        for i in range(3):
            v = vals[i]
            if v is None:
                continue
            if self._is_race:
                lap = self._lap.get(num)
                ref = self._leader_sec.get(lap, [None, None, None])[i] if lap is not None else None
            else:
                ref = self._best_sec[i]
            out[i] = "purple" if ref is None else self._band(v - ref)
        return out

    def _emit(self, num: str, clock_time: datetime) -> None:
        colours = self._colours_for(num)
        key = tuple(colours)
        if self._emitted.get(num) == key:
            return
        self._emitted[num] = key
        self._bus.emit(f"driverSectorColour:{num}", colours, clock_time)
