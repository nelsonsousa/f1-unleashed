"""
Lap Delta Processor — live time delta vs the driver's best lap (P/Q only).

Subscribes to: liveTelemetry:{num}, telemetryLap:{num}:{lap}, driverLaps:{num}
Emits: driverDelta:{num}  { deltaMs, lap, trackPct }   (persist=False)

For every live telemetry sample, compares the current lap's elapsed time to the
driver's best lap at the SAME track point:
  - current elapsed = liveTelemetry.lapElapsedMs (zeroed at the lap's S/F crossing);
  - best elapsed   = the best lap's t_ms interpolated at the live sample's dp
    (telemetryLap sample = [dp, …, t_ms]);
  - deltaMs = currentElapsed - bestElapsed.

Reliability gate: do NOT interpolate (→ no emit) if the bracketing best-lap
samples are more than 1% track distance away on either side of the live dp.
If the driver has no best lap yet, delta is null → no emit. Delta is never
persisted (live-only); lap_classification re-derives the percentage from the
published best-lap time. Does not run for race sessions.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

RELIABILITY_GAP_PCT = 1.0


class LapDeltaProcessor(Processor):
    """Live delta to the driver's best lap, sampled by track position."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._enabled = session_type in ("practice", "qualifying")
        self._laps: dict[str, dict[int, list]] = {}    # num -> {lap: [(dp, t_ms)]}
        self._best_num: dict[str, int] = {}             # num -> best lap number
        self._best_curve: dict[str, list] = {}          # num -> sorted [(dp, t_ms)]

    def subscribe(self) -> None:
        if self._enabled:
            self._bus.on("*", self._handle)

    def _handle(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("liveTelemetry:"):
            self._on_live(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("telemetryLap:"):
            parts = topic.split(":")
            if len(parts) == 3:
                try:
                    self._on_lap(parts[1], int(parts[2]), data)
                except ValueError:
                    pass
        elif topic.startswith("driverLaps:"):
            self._on_driver_laps(topic.split(":", 1)[1], data)

    def _on_lap(self, num: str, lap: int, samples: Any) -> None:
        if not isinstance(samples, list):
            return
        curve = [(s[0], s[6]) for s in samples
                 if len(s) >= 7 and s[0] is not None and s[6] is not None]
        self._laps.setdefault(num, {})[lap] = curve
        if self._best_num.get(num) == lap:
            self._build_curve(num)

    def _on_driver_laps(self, num: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        bl = data.get("bestLap")
        b = bl.get("lap") if isinstance(bl, dict) else None
        if isinstance(b, int) and b != self._best_num.get(num):
            self._best_num[num] = b
            self._build_curve(num)

    def _build_curve(self, num: str) -> None:
        curve = self._laps.get(num, {}).get(self._best_num.get(num))
        if curve:
            self._best_curve[num] = sorted(curve, key=lambda x: x[0])

    def _on_live(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        curve = self._best_curve.get(num)
        if not curve:
            return   # no best lap yet → delta null, not emitted
        dp = data.get("dp")
        elapsed = data.get("lapElapsedMs")
        if dp is None or elapsed is None:
            return
        best_t = self._interp(curve, dp)
        if best_t is None:
            return   # bracketing best samples too far → unreliable, not emitted
        self._bus.emit(f"driverDelta:{num}", {
            "deltaMs": int(elapsed - best_t),
            "lap": data.get("lap"),
            "trackPct": dp,
        }, clock_time, persist=False)

    @staticmethod
    def _interp(curve: list, dp: float) -> Optional[float]:
        lo = hi = None
        for d, t in curve:
            if d <= dp and (lo is None or d > lo[0]):
                lo = (d, t)
            if d >= dp and (hi is None or d < hi[0]):
                hi = (d, t)
        if lo is None or hi is None:
            return None
        if (dp - lo[0]) > RELIABILITY_GAP_PCT or (hi[0] - dp) > RELIABILITY_GAP_PCT:
            return None
        if hi[0] == lo[0]:
            return lo[1]
        frac = (dp - lo[0]) / (hi[0] - lo[0])
        return lo[1] + (hi[1] - lo[1]) * frac
