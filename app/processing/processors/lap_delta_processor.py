"""
Lap Delta Processor — live time delta vs the driver's best lap (P/Q only).

Subscribes to: liveTelemetry:{num}, telemetryLap:{num}:{lap}, driverLaps:{num}
Emits: driverDelta:{num}  { deltaMs, lap, trackPct }

For every live telemetry sample, compares the current lap's elapsed time to the
driver's best lap at the SAME track point:
  - current elapsed = liveTelemetry.lapElapsedMs (zeroed at the lap's S/F crossing);
  - best elapsed    = the best lap's t_ms interpolated at the live sample's dp
    (telemetryLap sample = [dp, …, t_ms]);
  - deltaMs = currentElapsed - bestElapsed.

Reference selection (avoids a race condition): driverLaps gives the best lap
NUMBER, but when the best lap is a fresh personal best its NoL/bestLap update can
arrive at (or slightly before) the telemetry samples that close it. So the
reference curve is rebuilt only once BOTH the best lap is known AND that lap's
telemetryLap has been cached — never against a stale lap.

Gates (no emit):
  - no best lap yet, or its telemetryLap not cached yet;
  - elapsed < MIN_ELAPSED_MS (the first seconds of a lap are meaningless);
  - bracketing best-lap samples > 1% track distance away either side (outage).
Does not run for race sessions.
"""

from datetime import datetime
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

RELIABILITY_GAP_PCT = 1.0
MIN_ELAPSED_MS = 5_000


class LapDeltaProcessor(Processor):
    """Live delta to the driver's best lap, sampled by track position."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._enabled = session_type in ("practice", "qualifying")
        self._laps: dict[str, dict[int, list]] = {}    # num -> {lap: [(dp, t_ms)]}
        self._best_num: dict[str, int] = {}             # num -> best lap number (driverLaps)
        self._best_curve: dict[str, list] = {}          # num -> sorted [(dp, t_ms)]
        self._best_curve_lap: dict[str, int] = {}       # num -> lap the curve was built for

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
        self._laps.setdefault(num, {})[lap] = [
            (s[0], s[6]) for s in samples
            if len(s) >= 7 and s[0] is not None and s[6] is not None
        ]

    def _on_driver_laps(self, num: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        # Reference the SESSION-WIDE best (overallBestLap), not the per-part
        # bestLap — the delta predictor needs the overall benchmark lap, which
        # is kept across qualifying parts (card 63). Equal outside quali.
        bl = data.get("overallBestLap")
        b = bl.get("lap") if isinstance(bl, dict) else None
        if isinstance(b, int):
            self._best_num[num] = b

    def _ref_curve(self, num: str) -> Optional[list]:
        """The current best lap's curve, rebuilt only once its telemetryLap is
        cached — so a freshly-set best never resolves to a stale reference."""
        bn = self._best_num.get(num)
        if bn is None:
            return None
        if self._best_curve_lap.get(num) != bn:
            c = self._laps.get(num, {}).get(bn)
            if not c:
                return None   # best lap known but its telemetry not cached yet
            self._best_curve[num] = sorted(c, key=lambda x: x[0])
            self._best_curve_lap[num] = bn
        return self._best_curve[num]

    def _on_live(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        dp = data.get("dp")
        elapsed = data.get("lapElapsedMs")
        if dp is None or elapsed is None or elapsed < MIN_ELAPSED_MS:
            return
        curve = self._ref_curve(num)
        if not curve:
            return
        best_t = self._interp(curve, dp)
        if best_t is None:
            return
        self._bus.emit(f"driverDelta:{num}", {
            "deltaMs": int(elapsed - best_t),
            "refMs": int(best_t),   # reference lap's time to THIS point (SLOW ratio denominator)
            "lap": data.get("lap"),
            "trackPct": dp,
        }, clock_time)

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
