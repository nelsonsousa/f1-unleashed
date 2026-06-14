"""
Lap Prediction Processor — Qualifying only (SME redesign — 2026-06-10).

Subscribes to: driverDelta:{num}, driverLapClassification:{num},
               driverLaps:{num}, qualifyingSegment
Emits: lapPrediction:{num}  { lap, delta, placesGained, observed }   (persisted)

While a driver is on a PUSH lap and the live delta to their best lap is negative
(on course to improve), projects the predicted lap time = bestLapMs + deltaMs
and ranks it against every non-knocked-out driver's current best lap. Reports
how many places that improvement would gain over the driver's current position
(observed=false). When that push lap completes, re-emits the ACTUAL outcome —
delta = completed lap − reference best, plus the real places gained
(observed=true, cards 62/67). New live predictions are then gated briefly so the
result stays readable (card 67).

  - best laps from lap_timing's driverLaps.bestLap.time;
  - knocked-out set from standings' qualifyingSegment.eliminated;
  - only PUSH laps (driverLapClassification) with delta < 0 produce a prediction.
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


class LapPredictionProcessor(Processor):

    # After a push lap's OBSERVED result is emitted, hold off new live
    # predictions this long (session seconds) so the user can read it (card 67).
    _GATE_S = 5.0

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type == "qualifying"
        self._best: dict[str, int] = {}          # num -> best lap ms (all drivers)
        self._cur_lap: dict[str, int] = {}        # num -> current lap (classification)
        self._cls: dict[str, str] = {}            # num -> current lap type
        self._eliminated: set[str] = set()
        # Live-prediction → observed-result bookkeeping (cards 62/67).
        self._pending_lap: dict[str, int] = {}    # num -> lap a live prediction is active for
        self._ref_best: dict[str, int] = {}       # num -> best ms the pending lap is measured against
        self._gate_until: dict[str, float] = {}   # num -> session epoch sec until which predictions are gated

    def subscribe(self) -> None:
        if self._active:
            self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLapClassification:"):
            self._on_cls(topic.split(":", 1)[1], data)
        elif topic == "qualifyingSegment":
            if isinstance(data, dict) and isinstance(data.get("eliminated"), list):
                self._eliminated = set(data["eliminated"])
        elif topic.startswith("driverDelta:"):
            self._on_delta(topic.split(":", 1)[1], data, clock_time)

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        # Observed result: the push lap we issued a live prediction for has now
        # completed — replace it with the ACTUAL delta + places gained (cards
        # 62/67). Computed BEFORE _best updates below, against the reference best
        # captured while predicting (overallBestLap would by now include this
        # lap if it was a PB).
        pend = self._pending_lap.get(num)
        ll = data.get("lastLap")
        if (pend is not None and isinstance(ll, dict)
                and ll.get("lap") == pend and ll.get("time")):
            last_ms = _parse_ms(ll["time"])
            ref = self._ref_best.get(num)
            if last_ms is not None and ref is not None:
                others = [m for d, m in self._best.items()
                          if d != num and d not in self._eliminated]
                rank_before = 1 + sum(1 for o in others if o < ref)
                rank_after = 1 + sum(1 for o in others if o < last_ms)
                self._bus.emit(f"lapPrediction:{num}", {
                    "lap": pend,
                    "delta": last_ms - ref,           # actual delta vs the reference lap
                    "placesGained": rank_before - rank_after,
                    "observed": True,
                }, clock_time)
            self._pending_lap.pop(num, None)
            self._ref_best.pop(num, None)
            # Hold off new predictions briefly so the result is readable (card 67).
            self._gate_until[num] = clock_time.timestamp() + self._GATE_S

        # Session-wide best (overallBestLap): predicted = bestMs + deltaMs, and
        # deltaMs is measured against the overall reference lap (lap_delta), so
        # the benchmark here must match it (card 63).
        bl = data.get("overallBestLap")
        if isinstance(bl, dict) and bl.get("time"):
            ms = _parse_ms(bl["time"])
            if ms is not None:
                self._best[num] = ms

    def _on_cls(self, num: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        lap = data.get("lap")
        # Only the current (highest) lap drives the gate — ignore Rule 1
        # reclassifications of earlier laps.
        if isinstance(lap, int) and lap >= self._cur_lap.get(num, 0):
            self._cur_lap[num] = lap
            self._cls[num] = data.get("type")

    def _on_delta(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if self._cls.get(num) != "PUSH" or num in self._eliminated:
            return
        # Gate: just after the previous push lap's observed result, hold off new
        # live predictions so the user can read it (card 67).
        if clock_time.timestamp() < self._gate_until.get(num, 0.0):
            return
        delta = data.get("deltaMs")
        best = self._best.get(num)
        if delta is None or delta >= 0 or best is None:
            return
        predicted = best + delta
        others = [ms for d, ms in self._best.items()
                  if d != num and d not in self._eliminated]
        predicted_rank = 1 + sum(1 for o in others if o < predicted)
        current_rank = 1 + sum(1 for o in others if o < best)
        self._bus.emit(f"lapPrediction:{num}", {
            "lap": data.get("lap"),
            "delta": delta,
            "placesGained": current_rank - predicted_rank,
            "observed": False,
        }, clock_time)
        # Remember the lap + reference best so we can emit the observed result
        # when this lap completes (cards 62/67).
        self._pending_lap[num] = data.get("lap")
        self._ref_best[num] = best
