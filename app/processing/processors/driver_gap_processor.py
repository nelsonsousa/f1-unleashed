"""
Driver Gap Processor — gap to leader + interval to the car ahead.

Subscribes to: TimingData
Emits (pure pass-through of the source values):
  driverGap:{num}   race: GapToLeader            ; P/Q: Stats TimeDiffToFastest
  driverInt:{num}   race: IntervalToPositionAhead; P/Q: Stats TimeDifftoPositionAhead

Split out of timing_processor (which emitted these for race only). P/Q is now
covered too.

NOTE: P/Q gaps come from the Stats array (F1 fields TimeDiffToFastest /
TimeDifftoPositionAhead — the latter has a lower-case 't' typo at source). The
Stats array's index semantics are unconfirmed; we use the last entry carrying a
value. VERIFY against the standings tile before relying on the P/Q values.
"""

from datetime import datetime
from typing import Any

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


class DriverGapProcessor(Processor):
    """Emits per-driver gap-to-leader and interval-to-ahead."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._gap: dict[str, Any] = {}
        self._int: dict[str, Any] = {}

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if self._is_race:
                if "GapToLeader" in d:
                    self._emit_gap(num, d["GapToLeader"], clock_time)
                if "IntervalToPositionAhead" in d:
                    v = d["IntervalToPositionAhead"]
                    if isinstance(v, dict):
                        v = v.get("Value", "")
                    self._emit_int(num, v, clock_time)
            else:
                stats = d.get("Stats")
                if isinstance(stats, (list, dict)):
                    entries = stats.values() if isinstance(stats, dict) else stats
                    gap = intv = None
                    for e in entries:
                        if not isinstance(e, dict):
                            continue
                        if e.get("TimeDiffToFastest"):
                            gap = e["TimeDiffToFastest"]
                        if e.get("TimeDifftoPositionAhead"):
                            intv = e["TimeDifftoPositionAhead"]
                    if gap is not None:
                        self._emit_gap(num, gap, clock_time)
                    if intv is not None:
                        self._emit_int(num, intv, clock_time)

    def _emit_gap(self, num: str, v: Any, clock_time: datetime) -> None:
        if self._gap.get(num) != v:
            self._gap[num] = v
            self._bus.emit(f"driverGap:{num}", v, clock_time)

    def _emit_int(self, num: str, v: Any, clock_time: datetime) -> None:
        if self._int.get(num) != v:
            self._int[num] = v
            self._bus.emit(f"driverInt:{num}", v, clock_time)
