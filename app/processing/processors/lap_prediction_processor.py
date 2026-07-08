"""
Lap Prediction Processor — Qualifying only (SME rework 2026-07-08).

Subscribes to: driverDelta:{num}, driverLapClassification:{num}, driverLaps:{num},
               driverStatus:{num}, qualifyingSegment, qualifyingPart
Emits: lapPrediction:{num}
       { lap, delta, placesGained, predictedPos, deltaColour, posColour }

While a driver is on a PUSH lap, projects predicted lap time = reference lap
(lap_delta.refFullMs = the driver's personal-best reference) + live delta, and ranks
it against every non-eliminated driver's best-THIS-part (the live quali order):

  - driver HAS a best this part → publish delta (ms) + placesGained (>=0);
  - no best this part           → publish predictedPos (Pxx), no delta.

Colours (server): delta green if improving (<0) else yellow; the position — places
gained OR predicted — is coloured by the BAND the predicted lap time earns, using the
same P/Q pace bands (purple = would be fastest, blue/green/yellow by Δ, red = slower
than the bubble). The client renders only: gains → coloured up-arrow, =0 → white,
Pnn → coloured.

Runs on PUSH laps only. PUSH→SLOW/out, pit/retire, or lap completion → clear (blank);
no observed result is published (nothing to gain there).
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


_BLANK = {"lap": None, "delta": None, "predictedPos": None,
          "deltaColour": None, "posColour": None}

MIN_TRACK_PCT = 10.0   # don't publish a prediction before 10% of the lap (user 2026-07-08)


class LapPredictionProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type == "qualifying"
        self._part_best: dict[str, int] = {}      # num -> best-THIS-part ms (ranking + has-lap)
        self._cur_lap: dict[str, int] = {}
        self._cls: dict[str, str] = {}
        self._eliminated: set[str] = set()
        self._part: Optional[int] = None
        self._status: dict[str, Any] = {}
        self._pending: dict[str, int] = {}         # num -> lap a prediction is active for
        self._emitted: dict[str, Optional[tuple]] = {}   # dedup

    def subscribe(self) -> None:
        if self._active:
            self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverDelta:"):
            self._on_delta(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLapClassification:"):
            self._on_cls(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverLaps:"):
            self._on_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:"):
            self._on_status(topic.split(":", 1)[1], data, clock_time)
        elif topic == "qualifyingSegment":
            if isinstance(data, dict) and isinstance(data.get("eliminated"), list):
                self._eliminated = set(data["eliminated"])
        elif topic == "qualifyingPart":
            if isinstance(data, int) and data != self._part:
                self._part = data
                self._part_best.clear()            # best-this-part resets each part

    def _on_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        bl = data.get("bestLap")
        if isinstance(bl, dict) and bl.get("part") == self._part and bl.get("time"):
            ms = _parse_ms(bl["time"])
            if ms is not None:
                self._part_best[num] = ms
        else:
            self._part_best.pop(num, None)         # no best this part (null / earlier part)
        # Lap complete → clear (no observed result). (user 2026-07-08)
        pend = self._pending.get(num)
        ll = data.get("lastLap")
        if pend is not None and isinstance(ll, dict) and ll.get("lap") == pend:
            self._clear(num, clock_time)

    def _on_cls(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        lap = data.get("lap")
        if isinstance(lap, int) and lap >= self._cur_lap.get(num, 0):
            prev = self._cls.get(num)
            self._cur_lap[num] = lap
            self._cls[num] = data.get("type")
            # PUSH → SLOW / out → blank and stop.
            if prev == "PUSH" and self._cls[num] != "PUSH" and num in self._pending:
                self._clear(num, clock_time)

    def _on_status(self, num: str, data: Any, clock_time: datetime) -> None:
        st = data if isinstance(data, str) else None
        prev = self._status.get(num)
        self._status[num] = st
        if st in ("PIT", "RET", "STOP") and prev not in ("PIT", "RET", "STOP"):
            self._clear(num, clock_time)

    def _clear(self, num: str, clock_time: datetime) -> None:
        self._pending.pop(num, None)
        if self._emitted.get(num) is not None:
            self._emitted[num] = None
            self._bus.emit(f"lapPrediction:{num}", dict(_BLANK), clock_time)

    # ── ranking / colour helpers (best-this-part = live quali order) ──
    def _others(self, num: str) -> list:
        return [m for d, m in self._part_best.items()
                if d != num and d not in self._eliminated]

    def _bubble_ms(self) -> Optional[int]:
        cut = 16 if self._part == 1 else 10 if self._part == 2 else None
        vals = sorted(m for d, m in self._part_best.items() if d not in self._eliminated)
        return vals[cut - 1] if cut is not None and len(vals) >= cut else None

    def _band(self, predicted: int) -> str:
        """Band the predicted lap time earns vs the fastest-this-part + bubble."""
        vals = [m for d, m in self._part_best.items() if d not in self._eliminated]
        ref = min(vals) if vals else None
        if ref is None or predicted <= ref:
            return "purple"
        bubble = self._bubble_ms()
        if bubble is not None and predicted > bubble:
            return "red"
        d = (predicted - ref) / 1000.0
        if d < 0.2:
            return "blue"
        if d < 0.5:
            return "green"
        if d < 1.0:
            return "yellow"
        return "orange"

    def _on_delta(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        if self._cls.get(num) != "PUSH" or num in self._eliminated:
            return
        if (data.get("trackPct") or 0) < MIN_TRACK_PCT:
            return
        delta = data.get("deltaMs")
        ref_full = data.get("refFullMs")
        if delta is None or ref_full is None:
            return
        predicted = ref_full + delta
        others = self._others(num)
        payload = dict(_BLANK)
        payload["lap"] = data.get("lap")
        # Predict the position ONLY for an improving lap (delta<0) — a slower lap makes
        # no meaningful prediction. A driver with a lap this part still shows the (yellow)
        # delta; one with no lap yet shows nothing. (user 2026-07-08)
        if delta < 0:
            payload["predictedPos"] = 1 + sum(1 for o in others if o < predicted)
            payload["posColour"] = self._band(predicted)
        if self._part_best.get(num) is not None:
            payload["delta"] = delta
            payload["deltaColour"] = "green" if delta < 0 else "yellow"
        key = (payload["delta"], payload["predictedPos"],
               payload["deltaColour"], payload["posColour"])
        if self._emitted.get(num) == key:
            return
        self._emitted[num] = key
        self._pending[num] = data.get("lap")
        self._bus.emit(f"lapPrediction:{num}", payload, clock_time)
