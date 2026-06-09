"""
Lap Timing Processor — authoritative per-driver lap count + lap times.

Subscribes to: SessionStatus, TimingData, LapCount (race)
Emits:
  driverLaps:{num}  per-driver lap record:
      { currentLap, laps:{ "n":{time, personalBest, overallBest} },
        lastLap:{lap,time,personalBest,overallBest}|null, bestLap:{lap,time}|null }
  raceLaps          (race only) { currentLap, totalLaps }  — from LapCount

Model (validated against 2026 data):
  - NumberOfLaps (NoL) is the authoritative per-driver lap counter and
    increments by +1 (one >+1 case across 30 sessions, a known source merge).
  - Lap *number* a NoL completes differs by session type:
        practice/qualifying : NoL=N means lap N is STARTING -> completes N-1
        race/sprint         : NoL=N means lap N has ENDED   -> completes N
    We index laps by the COMPLETED lap number (NoL-1 in P/Q, NoL in race).
  - LastLapTime pairing (handles out-of-order arrival):
      bundled (same msg as NoL)        -> the just-completed (highest) lap
      standalone, prev lap has no time -> assign to the just-completed lap
      standalone, prev lap has a time  -> for the in-progress lap; hold pending
        until the next NoL confirms its boundary.
  - Skipped NoL (e.g. 23->25): create empty slots for the skipped laps (they
    were driven but not recorded); time lands on the highest per the rules.
  - Lap counting only starts once the session has started (SessionStatus).
  - Red flags need no special handling — NoL/LastLapTime keep arriving normally.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor


def _parse_ms(s: str) -> Optional[int]:
    """Parse 'M:SS.mmm' (or 'MM:SS.mmm') into milliseconds."""
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


class LapTimingProcessor(Processor):
    """Authoritative lap count + lap times per driver."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"   # "race" covers race + sprint
        self._started = False
        self._nol: dict[str, int] = {}                       # num -> current NoL
        self._laps: dict[str, dict[int, dict]] = {}          # num -> {lap -> {time,pb,ob}}
        self._pending: dict[str, dict] = {}                  # num -> held lap-time
        self._best: dict[str, dict] = {}                     # num -> {lap,time,ms}
        self._current_race_lap: Optional[int] = None
        self._total_race_laps: Optional[int] = None

    def subscribe(self) -> None:
        self._bus.on("SessionStatus", self._handle_session_status)
        self._bus.on("TimingData", self._handle_timing)
        if self._is_race:
            self._bus.on("LapCount", self._handle_lap_count)

    # ── Session gate ──
    def _handle_session_status(self, data: Any, clock_time: datetime) -> None:
        if isinstance(data, dict) and data.get("Status") == "Started":
            self._started = True   # latches True on the first start (stays set across Q1/Q2/Q3, restarts)

    # ── Race lap counter ──
    def _handle_lap_count(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if data.get("TotalLaps"):
            self._total_race_laps = data["TotalLaps"]
        if "CurrentLap" in data:
            self._current_race_lap = data["CurrentLap"]
            self._bus.emit("raceLaps", {
                "currentLap": self._current_race_lap,
                "totalLaps": self._total_race_laps,
            }, clock_time)

    # ── Lap timing ──
    def _completed(self, nol: Optional[int]) -> int:
        """Number of laps completed when the counter reads `nol`."""
        if nol is None:
            return 0
        return nol if self._is_race else nol - 1

    def _handle_timing(self, data: Any, clock_time: datetime) -> None:
        if not self._started:    # laps only count once the session has started
            return
        lines = data.get("Lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict):
            return
        changed = set()
        for num, d in lines.items():
            if isinstance(d, dict) and self._process(num, d):
                changed.add(num)
        for num in changed:
            self._emit(num, clock_time)

    def _process(self, num: str, d: dict) -> bool:
        ll = None
        llt = d.get("LastLapTime")
        if isinstance(llt, dict) and llt.get("Value"):
            ll = {"time": llt["Value"],
                  "personalBest": bool(llt.get("PersonalFastest")),
                  "overallBest": bool(llt.get("OverallFastest"))}
        changed = False
        if "NumberOfLaps" in d:
            changed |= self._advance(num, int(d["NumberOfLaps"]), ll)
            ll = None   # consumed as the bundled time
        if ll is not None:
            changed |= self._standalone(num, ll)
        return changed

    def _advance(self, num: str, new_nol: int, bundled_ll: Optional[dict]) -> bool:
        prev = self._nol.get(num)
        self._nol[num] = new_nol
        laps = self._laps.setdefault(num, {})
        new_c = self._completed(new_nol)
        prev_c = self._completed(prev)
        # Create a slot for every newly-completed lap (skipped laps get an
        # empty slot — driven but not recorded by the source).
        for lap in range(max(prev_c, 0) + 1, new_c + 1):
            laps.setdefault(lap, {"time": None, "personalBest": False, "overallBest": False})
        # Assign the time of the just-completed (highest) lap.
        if new_c >= 1:
            if bundled_ll:
                self._set_time(num, new_c, bundled_ll)
            elif num in self._pending:
                self._set_time(num, new_c, self._pending.pop(num))
        return True

    def _standalone(self, num: str, ll: dict) -> bool:
        c = self._completed(self._nol.get(num))
        laps = self._laps.get(num, {})
        if c >= 1 and laps.get(c, {}).get("time") is None:
            self._set_time(num, c, ll)   # the just-completed lap was still timeless
            return True
        # the prev lap already has a time -> this is the in-progress lap; hold it
        self._pending[num] = ll
        return False

    def _set_time(self, num: str, lap: int, ll: dict) -> None:
        self._laps[num][lap] = dict(ll)
        ms = _parse_ms(ll["time"])
        if ms is not None and (num not in self._best or ms < self._best[num]["ms"]):
            self._best[num] = {"lap": lap, "time": ll["time"], "ms": ms}

    def _emit(self, num: str, clock_time: datetime) -> None:
        laps = self._laps.get(num, {})
        timed = [l for l, v in laps.items() if v.get("time")]
        last = max(timed) if timed else None
        best = self._best.get(num)
        self._bus.emit(f"driverLaps:{num}", {
            "currentLap": self._nol.get(num),
            "laps": {str(l): laps[l] for l in sorted(laps)},
            "lastLap": ({"lap": last, **laps[last]} if last is not None else None),
            "bestLap": ({"lap": best["lap"], "time": best["time"]} if best else None),
        }, clock_time)
