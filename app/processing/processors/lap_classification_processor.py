"""
Lap Classification Processor (SME redesign — 2026-06-10).

Subscribes to: driverLaps:{num}, driverStatus:{num}, driverDelta:{num},
               liveTelemetry:{num}, currentTyre:{num}
Emits: driverLapClassification:{num}  { lap, trackPct, type }   (persisted)

Type per lap (priority, highest first):
    STOP   driver stopped on track (overlay; reverts when STOP is revoked)
    PIT    lap entered/ended in the pits          (driverStatus PIT)
    OUT    lap started from the pits              (driverStatus OUT)
    SLOW   timed lap whose delta blew out          (P/Q only)
    PUSH   timed lap, default
    ""     race lap (empty — not shown on client)

IN/OUT/TIMED resolution: PIT > OUT > timed, so a lap that starts OUT and ends in
the pits is PIT; start OUT / end at S/F is OUT; start S/F / end in pits is PIT;
start and end at S/F is timed (PUSH/SLOW for P/Q, "" for race).

TIMED laps (P/Q):
  - PUSH by default; SLOW when, between 10%–90% track distance, delta blows out:
    deltaPct = driverDelta.deltaMs / driverLaps.bestLap.time × 100, SLOW iff
    deltaPct > 20 AND deltaMs > 5000. Before 10% stays PUSH; after 90% no flips.
  - Rule 1 (multi-lap prep): at the start of a timed lap N, walking back from
    N-2 while each prior lap is PUSH, reclassify it SLOW if its lap time exceeds
    lapTime(N-1) + 10 s (guarded by N-1 being a timed lap). Re-emits the lap.
  - Rule 2 (wet): on Intermediate/Wet tyres there are no SLOW laps and no
    reclassification — all timed laps are PUSH.

Race laps are "" unless PIT/OUT, with one exception: driverStatus STOP → STOP
(reverts to "" when STOP is revoked).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

PREP_GAP_MS = 10_000
SLOW_PCT = 20.0
SLOW_MIN_MS = 5_000
WINDOW_LO = 10.0
WINDOW_HI = 90.0
WET = {"INTERMEDIATE", "WET"}


def _parse_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


@dataclass
class DriverCls:
    cur_lap: Optional[int] = None
    last_status: str = ""             # latest driverStatus value
    went_out: bool = False
    went_pit: bool = False
    stopped: bool = False
    slow: bool = False                # SLOW detected for the current lap
    last_dp: float = 0.0
    best_ms: Optional[int] = None
    lap_times: dict = field(default_factory=dict)   # lap -> ms
    final_type: dict = field(default_factory=dict)  # lap -> locked classification
    wet: bool = False
    last_emit: dict = field(default_factory=dict)   # lap -> last emitted type


class LapClassificationProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._is_race = session_type == "race"
        self._is_pq = session_type in ("practice", "qualifying")
        self._drivers: dict[str, DriverCls] = {}

    def subscribe(self) -> None:
        self._bus.on("*", self._handle)

    def _drv(self, num: str) -> DriverCls:
        d = self._drivers.get(num)
        if d is None:
            d = DriverCls()
            self._drivers[num] = d
        return d

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:"):
            self._on_status(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverDelta:"):
            self._on_delta(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("liveTelemetry:"):
            self._on_live(topic.split(":", 1)[1], data)
        elif topic.startswith("currentTyre:"):
            self._on_tyre(topic.split(":", 1)[1], data)

    def _on_live(self, num: str, data: Any) -> None:
        if isinstance(data, dict) and data.get("dp") is not None:
            self._drv(num).last_dp = data["dp"]

    def _on_tyre(self, num: str, data: Any) -> None:
        if isinstance(data, dict) and data.get("compound"):
            self._drv(num).wet = data["compound"].upper() in WET

    # ── classification core ──
    def _type(self, d: DriverCls) -> str:
        if d.stopped:
            return "STOP"
        if d.went_pit:
            return "PIT"
        if d.went_out:
            return "OUT"
        if self._is_race:
            return ""
        return "SLOW" if d.slow else "PUSH"

    def _emit(self, num: str, d: DriverCls, clock_time: datetime,
              track_pct: Optional[float] = None) -> None:
        if d.cur_lap is None:
            return
        t = self._type(d)
        pct = d.last_dp if track_pct is None else track_pct
        if d.last_emit.get(d.cur_lap) == t:
            return
        d.last_emit[d.cur_lap] = t
        self._bus.emit(f"driverLapClassification:{num}",
                       {"lap": d.cur_lap, "trackPct": pct, "type": t}, clock_time)

    def _on_status(self, num: str, status: Any, clock_time: datetime) -> None:
        if not isinstance(status, str):
            return
        d = self._drv(num)
        d.last_status = status
        changed = False
        if status == "OUT" and not d.went_out:
            d.went_out = True; changed = True
        elif status == "PIT" and not d.went_pit:
            d.went_pit = True; changed = True
        elif status == "STOP" and not d.stopped:
            d.stopped = True; changed = True
        elif status != "STOP" and d.stopped:
            d.stopped = False; changed = True   # STOP revoked
        if changed:
            self._emit(num, d, clock_time)

    def _on_delta(self, num: str, data: Any, clock_time: datetime) -> None:
        if not self._is_pq or not isinstance(data, dict):
            return
        d = self._drv(num)
        dp = data.get("trackPct")
        delta = data.get("deltaMs")
        if dp is not None:
            d.last_dp = dp
        if d.slow or d.wet or d.went_out or d.went_pit or d.stopped:
            return
        if dp is None or delta is None or d.best_ms is None:
            return
        if not (WINDOW_LO <= dp <= WINDOW_HI):
            return
        if delta > SLOW_MIN_MS and (delta / d.best_ms * 100.0) > SLOW_PCT:
            d.slow = True
            self._emit(num, d, clock_time, track_pct=dp)

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        d = self._drv(num)
        bl = data.get("bestLap")
        if isinstance(bl, dict) and bl.get("time"):
            ms = _parse_ms(bl["time"])
            if ms is not None:
                d.best_ms = ms
        laps = data.get("laps")
        if isinstance(laps, dict):
            for k, v in laps.items():
                if isinstance(v, dict) and v.get("time"):
                    ms = _parse_ms(v["time"])
                    if ms is not None:
                        d.lap_times[int(k)] = ms
        cur = data.get("currentLap")
        if isinstance(cur, int) and cur != d.cur_lap:
            self._start_lap(num, d, cur, clock_time)

    def _start_lap(self, num: str, d: DriverCls, lap: int, clock_time: datetime) -> None:
        if d.cur_lap is not None:
            d.final_type[d.cur_lap] = self._type(d)
        d.cur_lap = lap
        # pit/out are per-lap. OUT (PitOut) fires just before the lap counter
        # increments, so the out-lap is detected from the latched status here.
        d.went_pit = False
        d.went_out = (d.last_status == "OUT")
        d.slow = False
        self._emit(num, d, clock_time, track_pct=0.0)
        if self._is_pq and not d.wet and self._type(d) == "PUSH":
            self._reclassify_prep(num, d, lap, clock_time)

    def _reclassify_prep(self, num: str, d: DriverCls, n: int,
                         clock_time: datetime) -> None:
        ref = d.lap_times.get(n - 1)
        if ref is None or d.final_type.get(n - 1) not in ("PUSH", "SLOW"):
            return
        k = n - 2
        while d.final_type.get(k) == "PUSH":
            lt = d.lap_times.get(k)
            if lt is None or lt <= ref + PREP_GAP_MS:
                break
            d.final_type[k] = "SLOW"
            if d.last_emit.get(k) != "SLOW":
                d.last_emit[k] = "SLOW"
                self._bus.emit(f"driverLapClassification:{num}",
                               {"lap": k, "trackPct": 100.0, "type": "SLOW"}, clock_time)
            k -= 1
