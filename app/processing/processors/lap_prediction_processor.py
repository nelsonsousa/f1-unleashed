"""
Lap Prediction Processor — Practice + Qualifying (SME rework 2026-07-15).

Subscribes to: driverDelta:{num}, driverLapClassification:{num}, driverLaps:{num},
               driverStatus:{num}, qualifyingSegment, qualifyingPart
Emits: lapPrediction:{num}
       { lap, projectedMs, improving, predictedPos, posColour }

While a driver is on a PUSH lap (predictions start past MIN_TRACK_PCT of the lap), projects the
PREDICTED LAP TIME = reference lap (lap_delta.refFullMs, the driver's personal-best reference) +
live delta, and publishes it every ~2 s regardless of sign — `projectedMs` (the client shows it to
0.1 s, e.g. 1:23.4 → 1:23.2). `improving` = the projected time beats the reference.

Position: rank the projected time in the live order (best-this-part of non-eliminated drivers) →
raw rank; the predicted position = min(raw, the driver's current position) — it never gets worse
than where they already are. `posColour` = the pace band the projected time earns when improving
(purple fastest / blue-green-yellow by Δ / red past the bubble), else WHITE.

Practice: no parts/eliminations, so best-this-part is best-this-session and there's no bubble.
Runs on PUSH laps only; PUSH→SLOW/out, pit/retire, or lap completion → clear (blank).
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


_BLANK = {"lap": None, "projectedMs": None, "improving": None,
          "predictedPos": None, "posColour": None}

MIN_TRACK_PCT = 20.0     # don't publish a prediction before 20% of the lap (user 2026-07-15)
EMIT_INTERVAL_S = 2.0    # publish at most once per 2 s (session clock)
MEDIAN_WINDOW = 4        # smooth the delta over the last 4 samples


class LapPredictionProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._active = session_type in ("qualifying", "practice")   # + practice (user 2026-07-15)
        self._part_best: dict[str, int] = {}      # num -> best-THIS-part ms (ranking + has-lap)
        self._cur_lap: dict[str, int] = {}
        self._cls: dict[str, str] = {}
        self._eliminated: set[str] = set()
        self._part: Optional[int] = None
        self._status: dict[str, Any] = {}
        self._pending: dict[str, int] = {}         # num -> lap a prediction is active for
        self._emitted: dict[str, Optional[tuple]] = {}   # dedup
        self._buf: dict[str, list] = {}            # num -> last <=4 raw deltaMs (median window)
        self._last_emit: dict[str, float] = {}     # num -> session ts of last publish (2 s throttle)
        self._buf_lap: dict[str, Any] = {}         # num -> lap the buffer belongs to
        self._last_pred: dict[str, dict] = {}      # num -> last emitted prediction (for the lapEnd echo)

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
        # Lap complete → emit a "lapEnd" status carrying the last predicted values, so the dashboard
        # switches from predicting to observed mode (actual lap time + position). (user 2026-07-15)
        pend = self._pending.get(num)
        ll = data.get("lastLap")
        if pend is not None and isinstance(ll, dict) and ll.get("lap") == pend:
            self._lap_end(num, clock_time)

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
        self._buf.pop(num, None)
        self._last_emit.pop(num, None)
        self._buf_lap.pop(num, None)
        self._last_pred.pop(num, None)
        if self._emitted.get(num) is not None:
            self._emitted[num] = None
            self._bus.emit(f"lapPrediction:{num}", dict(_BLANK), clock_time)

    def _lap_end(self, num: str, clock_time: datetime) -> None:
        """PUSH lap finished normally: echo the last prediction with status='lapEnd' (the dashboard
        then switches to the observed lap time + position)."""
        last = self._last_pred.get(num)
        if last is not None:
            self._bus.emit(f"lapPrediction:{num}", {**last, "status": "lapEnd"}, clock_time)
        self._pending.pop(num, None)
        self._buf.pop(num, None)
        self._last_emit.pop(num, None)
        self._buf_lap.pop(num, None)
        self._last_pred.pop(num, None)
        self._emitted[num] = None

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
        # Gate order (user 2026-07-15): track-distance gate → PUSH classification → emit — a forecast
        # goes out even with NO reference, so the session's first runner still gets one.
        dp = data.get("trackPct")
        if dp is None or dp < MIN_TRACK_PCT:
            return
        if self._cls.get(num) != "PUSH" or num in self._eliminated:
            return
        lap = data.get("lap")
        raw = data.get("deltaMs")
        ref_full = data.get("refFullMs")
        has_ref = raw is not None and ref_full is not None
        if lap != self._buf_lap.get(num):          # new lap → restart window + throttle
            self._buf_lap[num] = lap
            self._buf[num] = []
            self._last_emit.pop(num, None)
        if has_ref:
            buf = self._buf[num]
            buf.append(raw)
            if len(buf) > MEDIAN_WINDOW:
                buf.pop(0)
        now = clock_time.timestamp()               # throttle: at most once per 2 s
        if now - self._last_emit.get(num, -1e9) < EMIT_INTERVAL_S:
            return
        self._last_emit[num] = now
        if has_ref:
            buf = self._buf[num]
            delta = sorted(buf)[len(buf) // 2]     # median of the last <=4 samples
            predicted = ref_full + delta           # projected FULL lap time (ms)
            improving = delta < 0
            others = self._others(num)
            # Raw predicted rank = where the projected time slots into the current order; the predicted
            # position never gets WORSE than the driver's current position (min). (user 2026-07-15)
            rank = 1 + sum(1 for o in others if o < predicted)
            pb = self._part_best.get(num)
            cur = (1 + sum(1 for o in others if o < pb)) if pb is not None else len(others) + 1
            pred_pos = min(rank, cur)
            payload = {
                "lap": lap,
                "projectedMs": predicted,          # emitted on every PUSH sample, any sign (user)
                "improving": improving,
                "predictedPos": pred_pos,
                # Colour: with NO time set this part → always the pace band (any lap is the one that
                # counts). With a personal best → band while improving, white when not. (user 2026-07-15)
                "posColour": self._band(predicted) if (pb is None or improving) else "white",
            }
        else:
            # No reference yet: placeholder forecast — no time ("-:--.-"), provisional P1. (user 2026-07-15)
            payload = {
                "lap": lap,
                "projectedMs": None,
                "improving": None,
                "predictedPos": 1,
                "posColour": "purple",
            }
        key = (payload["projectedMs"], payload["predictedPos"], payload["improving"], payload["posColour"])
        if self._emitted.get(num) == key:
            return
        self._emitted[num] = key
        self._pending[num] = lap
        self._last_pred[num] = payload
        self._bus.emit(f"lapPrediction:{num}", payload, clock_time)
