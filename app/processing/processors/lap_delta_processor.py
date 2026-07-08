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


def _parse_ms(s: Any) -> Optional[int]:
    if not isinstance(s, str) or ":" not in s:
        return None
    try:
        mm, rest = s.split(":")
        sec, _, ms = rest.partition(".")
        return int(mm) * 60000 + int(sec) * 1000 + int((ms or "0").ljust(3, "0")[:3])
    except (ValueError, IndexError):
        return None


class LapDeltaProcessor(Processor):
    """Live delta to the driver's best lap, sampled by track position."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._enabled = session_type in ("practice", "qualifying")
        self._laps: dict[str, dict[int, list]] = {}    # num -> {lap: [(dp, t_ms)]}
        self._best_num: dict[str, int] = {}             # num -> own reference lap number (driverLaps)
        self._ref_full: dict[str, int] = {}             # num -> own reference lap's FULL time ms
        self._curves: dict[tuple, list] = {}            # (num, lap) -> sorted [(dp, t_ms)]
        self._fastest: Optional[tuple] = None           # session-fastest lap (num, lap, time_ms) — priority 3

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
        elif topic == "fastestLap":
            # Priority-3 reference: the session-fastest overall lap, for a driver with
            # NO lap of their own. Only updates when a new fastest is set. (user 2026-07-08)
            if isinstance(data, dict) and data.get("num") is not None and isinstance(data.get("lap"), int):
                t = _parse_ms(data.get("time"))
                if t is not None:
                    self._fastest = (data["num"], data["lap"], t)

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
        # Reference = the driver's PERSONAL BEST, by priority (user 2026-07-08):
        #   1. best lap THIS part (driverLaps.bestLap) if set;
        #   2. else best lap across all parts (overallBestLap).
        # (Priority 3 — another driver's lap when the driver has none — is handled
        #  in lap_prediction / TODO here for the curve.) Outside quali bestLap ==
        #  overallBestLap, so behaviour is unchanged there.
        ref = data.get("bestLap")
        if not (isinstance(ref, dict) and ref.get("lap") is not None):
            ref = data.get("overallBestLap")
        if isinstance(ref, dict) and isinstance(ref.get("lap"), int):
            self._best_num[num] = ref["lap"]
            t = _parse_ms(ref.get("time"))
            if t is not None:
                self._ref_full[num] = t

    def _build_curve(self, num: str, lap: Optional[int]) -> Optional[list]:
        """Sorted [(dp, t_ms)] for a driver's lap, cached by (num, lap). None until
        that lap's telemetryLap is cached — so a reference never resolves stale."""
        if lap is None:
            return None
        key = (num, lap)
        cached = self._curves.get(key)
        if cached is not None:
            return cached
        c = self._laps.get(num, {}).get(lap)
        if not c:
            return None
        curve = sorted(c, key=lambda x: x[0])
        self._curves[key] = curve
        return curve

    def _ref(self, num: str):
        """(curve, refFullMs) for the reference lap: the driver's own best (this-part
        or overall) if known, else the session-fastest overall lap (another driver's)."""
        bn = self._best_num.get(num)
        if bn is not None:
            curve = self._build_curve(num, bn)
            return (curve, self._ref_full.get(num)) if curve else (None, None)
        if self._fastest is not None:
            curve = self._build_curve(self._fastest[0], self._fastest[1])
            if curve:
                return curve, self._fastest[2]
        return None, None

    def _on_live(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        dp = data.get("dp")
        elapsed = data.get("lapElapsedMs")
        if dp is None or elapsed is None or elapsed < MIN_ELAPSED_MS:
            return
        curve, ref_full = self._ref(num)
        if not curve:
            return
        best_t = self._interp(curve, dp)
        if best_t is None:
            return
        self._bus.emit(f"driverDelta:{num}", {
            "deltaMs": int(elapsed - best_t),
            "refMs": int(best_t),   # reference lap's time to THIS point (SLOW ratio denominator)
            "refFullMs": ref_full,  # reference lap's FULL time → predicted = this + delta
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
