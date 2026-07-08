"""
Best-Sector Processor — each driver's fastest S1/S2/S3 + its band colour.

Subscribes to: driverSectors:{num}   [{value, overallFastest, personalFastest} x3]
Emits:
  driverBestSectors:{num}       [v1, v2, v3]   best sector time strings (or None)
  driverBestSectorColour:{num}  [c1, c2, c3]   band colour vs the session-best sector

The best sector is the driver's MINIMUM time per sector across the session (updated
whenever a driver posts a quicker sector). Colours reuse the same delta bands as the
live sector colours: purple = the session-fastest sector, then blue/green/yellow/
orange/red by Δ to the overall-fastest best sector. When the session best moves, the
whole field is recoloured. The client shows these when a sector column is toggled to
its "best" mode; they run in EVERY session type (the reference is always best-overall).
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_sector_ms(v: Any) -> Optional[int]:
    if not isinstance(v, str) or not v.strip():
        return None
    try:
        return round(float(v) * 1000)   # sector strings are seconds, e.g. "25.108"
    except ValueError:
        return None


class BestSectorProcessor(Processor):
    """Per-driver fastest sector time + band colour vs the session-best sector."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._best_ms: dict[str, list] = {}       # num -> [ms|None x3]  personal best per sector
        self._best_str: dict[str, list] = {}      # num -> [str|None x3] the winning value string
        self._overall: list = [None, None, None]  # session-best per sector (min across field)
        self._emitted_v: dict[str, tuple] = {}    # dedup values
        self._emitted_c: dict[str, tuple] = {}    # dedup colours

    def subscribe(self) -> None:
        self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverSectors:"):
            self._on_sectors(topic.split(":", 1)[1], data, clock_time)

    def _on_sectors(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, list):
            return
        best_ms = self._best_ms.setdefault(num, [None, None, None])
        best_str = self._best_str.setdefault(num, [None, None, None])
        improved = False
        overall_improved = False
        for i in range(min(3, len(data))):
            s = data[i]
            if not isinstance(s, dict):
                continue
            val = s.get("value")
            ms = _parse_sector_ms(val)
            if ms is None:
                continue
            if best_ms[i] is None or ms < best_ms[i]:
                best_ms[i] = ms
                best_str[i] = val
                improved = True
                if self._overall[i] is None or ms < self._overall[i]:
                    self._overall[i] = ms
                    overall_improved = True
        if improved:
            self._emit_values(num, clock_time)
        if overall_improved:
            for n in list(self._best_ms):            # session best moved → recolour field
                self._emit_colour(n, clock_time)
        elif improved:
            self._emit_colour(num, clock_time)

    def _band(self, delta_ms: int) -> str:
        if delta_ms <= 0:
            return "purple"                          # holds the session-fastest sector
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

    def _emit_values(self, num: str, clock_time: datetime) -> None:
        vals = self._best_str.get(num, [None, None, None])
        key = tuple(vals)
        if self._emitted_v.get(num) == key:
            return
        self._emitted_v[num] = key
        self._bus.emit(f"driverBestSectors:{num}", list(vals), clock_time)

    def _emit_colour(self, num: str, clock_time: datetime) -> None:
        best_ms = self._best_ms.get(num, [None, None, None])
        out = [None, None, None]
        for i in range(3):
            if best_ms[i] is None:
                continue
            ref = self._overall[i]
            out[i] = "purple" if ref is None else self._band(best_ms[i] - ref)
        key = tuple(out)
        if self._emitted_c.get(num) == key:
            return
        self._emitted_c[num] = key
        self._bus.emit(f"driverBestSectorColour:{num}", out, clock_time)
