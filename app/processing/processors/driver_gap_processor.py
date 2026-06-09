"""
Driver Gap Processor — gap column (+ race interval) per driver.

Subscribes to: TimingData
Emits:
  driverGap:{num}  {gap, cutoff}
      race       : gap = GapToLeader,             cutoff False
      practice   : gap = TimeDiffToFastest (direct per-line field), cutoff False
      qualifying : cutoff = driver is in the elimination zone (POSITIONAL —
                   not KnockedOut and Position past the cutoff). cutoff is NOT
                   F1's per-driver Cutoff flag (verified unreliable — it doesn't
                   track the drop zone). Cutoff position is fixed by the rules
                   (top 16 advance from Q1, top 10 from Q2 — always, regardless
                   of how many cars actually entered):
                       Q1: 16   (P17+ eliminated)
                       Q2: 10   (P11+ eliminated)
                       Q3: no cutoff
                   cutoff False -> gap = gap to P1   (Stats.TimeDiffToFastest)
                   cutoff True  -> gap = gap to cutoff (driverBest - CutOffTime)
                   gap = "" when the driver has no best lap yet.

  driverInt:{num}  interval to the car ahead — RACE ONLY (IntervalToPositionAhead)

SessionPart / CutOffTime are top-level TimingData fields. Because a
driver can be bumped into/out of the zone by someone else's lap, the whole quali
field is recomputed whenever any zone-relevant input changes (positions, bests,
CutOffTime, SessionPart, KnockedOut); the per-driver emit dedups.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


def _fmt_gap(ms: int) -> str:
    return f"{'+' if ms >= 0 else '-'}{abs(ms) / 1000:.3f}"


def _stats_timediff(stats: Any, key: str) -> Optional[str]:
    """Last non-empty `key` across the Stats array (quali gap fields)."""
    entries = stats.values() if isinstance(stats, dict) else stats
    val = None
    for e in entries:
        if isinstance(e, dict) and e.get(key):
            val = e[key]
    return val


class DriverGapProcessor(Processor):
    """Per-driver gap column (P1 / cutoff) and race interval."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._is_qualifying = session_type == "qualifying"
        # quali state
        self._cutoff_time_ms: Optional[int] = None
        self._session_part: Optional[int] = None
        self._pos: dict[str, int] = {}
        self._knocked: dict[str, bool] = {}
        self._best_ms: dict[str, int] = {}
        self._gap_p1: dict[str, str] = {}
        self._seen: set[str] = set()
        # emit dedup
        self._last_gap: dict[str, dict] = {}
        self._last_int: dict[str, Any] = {}

    def subscribe(self) -> None:
        self._bus.on("TimingData", self._handle)

    def _handle(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lines = data.get("Lines")
        if not isinstance(lines, dict):
            lines = {}
        if self._is_qualifying:
            self._handle_quali(data, lines, clock_time)
        elif self._is_race:
            self._handle_race(lines, clock_time)
        else:
            self._handle_practice(lines, clock_time)

    def _emit_gap(self, num: str, gap: Optional[str], cutoff: bool, clock_time: datetime) -> None:
        payload = {"gap": gap if gap is not None else "", "cutoff": cutoff}
        if payload != self._last_gap.get(num):
            self._last_gap[num] = payload
            self._bus.emit(f"driverGap:{num}", payload, clock_time)

    # ── Race ──
    def _handle_race(self, lines: dict, clock_time: datetime) -> None:
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "GapToLeader" in d:
                self._emit_gap(num, d["GapToLeader"], False, clock_time)
            if "IntervalToPositionAhead" in d:
                v = d["IntervalToPositionAhead"]
                if isinstance(v, dict):
                    v = v.get("Value", "")
                if v != self._last_int.get(num):
                    self._last_int[num] = v
                    self._bus.emit(f"driverInt:{num}", v, clock_time)

    # ── Practice ── (TimeDiffToFastest is a direct per-line field, no Stats)
    def _handle_practice(self, lines: dict, clock_time: datetime) -> None:
        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            if "TimeDiffToFastest" in d:
                self._emit_gap(num, d["TimeDiffToFastest"], False, clock_time)

    # ── Qualifying ── (positional elimination zone)
    def _cutoff_position(self) -> Optional[int]:
        # Fixed by the rules: top 16 advance from Q1, top 10 from Q2.
        if self._session_part == 1:
            return 16
        if self._session_part == 2:
            return 10
        return None    # Q3 / unknown — no cutoff

    def _handle_quali(self, data: dict, lines: dict, clock_time: datetime) -> None:
        changed = False
        if "CutOffTime" in data:
            ms = _parse_ms(data["CutOffTime"])
            if ms != self._cutoff_time_ms:
                self._cutoff_time_ms = ms; changed = True
        if "SessionPart" in data and data["SessionPart"] != self._session_part:
            self._session_part = data["SessionPart"]; changed = True

        for num, d in lines.items():
            if not isinstance(d, dict):
                continue
            self._seen.add(num)
            if "Position" in d:
                try:
                    p = int(d["Position"])
                    if p != self._pos.get(num):
                        self._pos[num] = p; changed = True
                except (TypeError, ValueError):
                    pass
            if "KnockedOut" in d:
                v = bool(d["KnockedOut"])
                if v != self._knocked.get(num):
                    self._knocked[num] = v; changed = True
            blt = d.get("BestLapTime")
            if isinstance(blt, dict) and blt.get("Value"):
                ms = _parse_ms(blt["Value"])
                if ms is not None and ms != self._best_ms.get(num):
                    self._best_ms[num] = ms; changed = True
            stats = d.get("Stats")
            if isinstance(stats, (list, dict)):
                g = _stats_timediff(stats, "TimeDiffToFastest")
                if g is not None and g != self._gap_p1.get(num):
                    self._gap_p1[num] = g; changed = True

        if not changed:
            return
        # Recompute the whole field — one driver's lap can flip another's zone.
        cutoff_pos = self._cutoff_position()
        for num in self._seen:
            in_zone = (cutoff_pos is not None
                       and not self._knocked.get(num)
                       and self._pos.get(num, 0) > cutoff_pos)
            if in_zone:
                bms = self._best_ms.get(num)
                gap = (_fmt_gap(bms - self._cutoff_time_ms)
                       if bms is not None and self._cutoff_time_ms is not None else "")
            else:
                gap = self._gap_p1.get(num, "")
            self._emit_gap(num, gap, in_zone, clock_time)
