"""
Lap Prediction Processor — Qualifying (and Sprint Qualifying) only.

Algorithm:

  Reference (= the lap to compare against):
    The driver's fastest completed PUSH lap so far in the current Q/SQ
    session. Updated whenever a new fast PUSH lap completes. The
    reference's lap-relative (distPct, t_ms) profile is read straight
    from TelemetryProcessor's `lapTelemetry:{num}:{lap}` emit at lap end.

  Live prediction (= every 2 s, after 10 s into the lap):
    distPct + clock_time arrive on the `position` topic. lap_start_ts
    is the wall-clock time of the most recent S/F wrap (= position drops
    from > 90% to < 10%). Lap-relative elapsed = clock_time − lap_start_ts.

      ref_t  = interp(ref_dists, ref_times, current_distPct)
      delta  = lap_relative_now − ref_t   (negative = faster)

    Smoothed across the last 2 s of raw per-position deltas (= median).
    Published at 0.1 s precision.

  Push-attempt gate:
    Predictions ONLY fire when the current lap's CLASSIFICATION (= the
    laps[max_key] from the most recent `lapClassification:{num}` emit)
    is PUSH. The classifier resets each new lap to PUSH and may later
    reclassify to COOL within the same lap; we observe that change in
    real time, so a lap reclassified as COOL stops getting predictions.
    A new S/F wrap unlocks the next lap's potential PUSH attempts.

Output topic `lapPrediction:{num}` payload:

  {
    "lap": int,                      # current lap number
    "distPct": float (1 decimal),
    "elapsed_ms": int,
    "delta_s": float (1 decimal),    # smoothed; UI shows this
    "delta_raw_s": float,            # latest raw, for debugging
    "predictedTimeMs": int,          # ref_actual + smoothed_delta
    "referenceLapMs": int,
    "smoothingWindowN": int,
  }

Position prediction is NOT computed here — it depends on every OTHER
driver's session best and must update whenever any of them improves.
The client owns that calculation.
"""
from __future__ import annotations

import logging
import statistics
from bisect import bisect_left
from datetime import datetime, timedelta
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)

EMIT_INTERVAL_S = 2.0
SMOOTHING_WINDOW_S = 2.0
MIN_LAP_ELAPSED_MS = 10_000

REF_MIN_SAMPLES = 20
REF_MIN_START_PCT = 5.0
REF_MIN_END_PCT = 95.0


def _parse_time_ms(s):
    if not s:
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        mins = int(parts[0])
        sp = parts[1].split(".")
        secs = int(sp[0])
        ms = int(sp[1].ljust(3, "0")[:3]) if len(sp) > 1 else 0
        return mins * 60_000 + secs * 1000 + ms
    except (ValueError, IndexError):
        return None


def _interp(xs, ys, x):
    if not xs:
        return None
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_left(xs, x)
    x0, x1 = xs[i - 1], xs[i]
    y0, y1 = ys[i - 1], ys[i]
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


class LapPredictionProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str,
                 telemetry_processor=None):
        super().__init__(bus, session_type)
        self._active = session_type == "qualifying"

        # Reference (= best PUSH lap so far this session, profile).
        self._ref_dists: dict[str, list[float]] = {}
        self._ref_times: dict[str, list[int]] = {}
        self._ref_actual_ms: dict[str, int] = {}
        self._ref_lap: dict[str, int] = {}

        # Raw inputs we need to promote a reference: completed-lap
        # samples (= lapTelemetry), reported lap time (= driverLapTimes),
        # PUSH classification (= lapClassification).
        self._lap_samples: dict[str, dict[int, list]] = {}
        self._lap_actual_ms: dict[str, dict[int, int]] = {}

        # Current-lap state per driver.
        # _cur_lap = max-keyed lap from the latest lapClassification;
        # _cur_cls = its classification ('PUSH'/'COOL'/'OUT'/'IN'/'PIT'/…).
        self._cur_lap: dict[str, int] = {}
        self._cur_cls: dict[str, str] = {}

        # Lap-start wall-clock timestamp, set on each S/F wrap from
        # position data. Predictions need this for elapsed_ms.
        self._lap_start_ts: dict[str, datetime] = {}
        self._prev_dist: dict[str, float] = {}

        # Smoothing + throttle.
        self._delta_buf: dict[str, list[tuple[datetime, float]]] = {}
        self._last_emit_ts: dict[str, datetime] = {}

    # ── Processor base API ────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "ref_actual_ms": dict(self._ref_actual_ms),
            "ref_lap": dict(self._ref_lap),
            "cur_lap": dict(self._cur_lap),
            "cur_cls": dict(self._cur_cls),
        }

    def restore(self, state: dict) -> None:
        self._ref_actual_ms = state.get("ref_actual_ms", {})
        self._ref_lap = state.get("ref_lap", {})
        self._cur_lap = state.get("cur_lap", {})
        self._cur_cls = state.get("cur_cls", {})
        self._ref_dists = {}
        self._ref_times = {}
        self._lap_samples = {}
        self._delta_buf = {}
        self._last_emit_ts = {}
        self._lap_start_ts = {}
        self._prev_dist = {}

    def reset(self) -> None:
        self._ref_dists.clear()
        self._ref_times.clear()
        self._ref_actual_ms.clear()
        self._ref_lap.clear()
        self._lap_samples.clear()
        self._lap_actual_ms.clear()
        self._cur_lap.clear()
        self._cur_cls.clear()
        self._lap_start_ts.clear()
        self._prev_dist.clear()
        self._delta_buf.clear()
        self._last_emit_ts.clear()

    def subscribe(self) -> None:
        if not self._active:
            return
        self._bus.on("*", self._on_any)

    # ── Topic dispatch ────────────────────────────────────────────────

    def _on_any(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic == "position":
            self._handle_position(data, clock_time)
        elif topic.startswith("lapTelemetry:"):
            parts = topic.split(":")
            if len(parts) == 3:
                try:
                    self._handle_lap_telemetry(parts[1], int(parts[2]), data)
                except ValueError:
                    return
        elif topic.startswith("driverLapTimes:"):
            self._handle_lap_times(topic.split(":", 1)[1], data)
        elif topic.startswith("lapClassification:"):
            self._handle_lap_cls(topic.split(":", 1)[1], data)

    # ── Reference building ────────────────────────────────────────────

    def _handle_lap_telemetry(self, num: str, lap: int, samples: Any) -> None:
        if not isinstance(samples, list) or len(samples) < REF_MIN_SAMPLES:
            return
        self._lap_samples.setdefault(num, {})[lap] = samples
        self._try_promote(num, lap)

    def _handle_lap_times(self, num: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        actual_map = self._lap_actual_ms.setdefault(num, {})
        for lap_str, time_str in data.items():
            try:
                lap = int(lap_str)
            except (ValueError, TypeError):
                continue
            ms = _parse_time_ms(time_str)
            if ms is not None and ms > 0:
                actual_map[lap] = ms
                self._try_promote(num, lap)

    def _try_promote(self, num: str, lap: int) -> None:
        """Promote a PUSH lap to the reference if it's a new session best
        and we have all three ingredients: samples + lap time + PUSH
        classification recorded for THIS lap."""
        # We don't track lapClassification per-lap; we trust that lapTelemetry
        # is only emitted for laps the driver actually drove. Whether the
        # lap was PUSH or COOL doesn't matter for the reference — we want
        # the FASTEST observed lap regardless of how the classifier
        # labelled it. (A "PUSH" reclassified mid-lap to COOL is still a
        # fast lap if it set the time.)
        actual = self._lap_actual_ms.get(num, {}).get(lap)
        if actual is None:
            return
        samples = self._lap_samples.get(num, {}).get(lap)
        if not samples or len(samples) < REF_MIN_SAMPLES:
            return
        cur_best = self._ref_actual_ms.get(num)
        if cur_best is not None and actual >= cur_best:
            return
        # lapTelemetry sample = [distPct, speed, rpm, gear, throttle, brake, lap_relative_t_ms]
        pts = []
        for s in samples:
            if isinstance(s, list) and len(s) >= 7:
                try:
                    pts.append((float(s[0]), int(s[6])))
                except (ValueError, TypeError):
                    continue
        if len(pts) < REF_MIN_SAMPLES:
            return
        pts.sort(key=lambda p: p[0])
        if pts[0][0] > REF_MIN_START_PCT or pts[-1][0] < REF_MIN_END_PCT:
            return
        max_t = max(p[1] for p in pts)
        if abs(max_t - actual) / max(actual, 1) > 0.05:
            return
        self._ref_dists[num] = [p[0] for p in pts]
        self._ref_times[num] = [p[1] for p in pts]
        self._ref_actual_ms[num] = actual
        self._ref_lap[num] = lap

    # ── Lap classification (= push-attempt gate) ──────────────────────

    def _handle_lap_cls(self, num: str, data: Any) -> None:
        """Per SME: every lapClassification emit identifies the most-
        recently-changed lap via the top-level `lap` + `status` fields.
        The `laps` dict (= full history) sometimes lags by one — at
        the very moment lap N's classification first emits, the dict
        may still only contain laps 1..N-1, so `max(laps.keys())` gives
        the wrong answer. Trust the top-level fields instead.
        """
        if not isinstance(data, dict):
            return
        lap = data.get("lap")
        status = data.get("status")
        if not isinstance(lap, int) or not isinstance(status, str) or lap < 1:
            return
        # The most recent classification change is for THIS lap. The
        # driver's current lap is the highest lap we've now seen.
        existing = self._cur_lap.get(num, 0)
        if lap >= existing:
            self._cur_lap[num] = lap
            self._cur_cls[num] = status

    # ── In-lap prediction (position-driven) ───────────────────────────

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, coords in data.items():
            if not isinstance(coords, list) or len(coords) < 3:
                continue
            self._handle_driver_position(num, float(coords[2]), clock_time)

    def _handle_driver_position(self, num: str, dist: float,
                                clock_time: datetime) -> None:
        # S/F wrap → reset lap_start_ts + smoothing buffer.
        prev = self._prev_dist.get(num)
        self._prev_dist[num] = dist
        if prev is not None and prev > 90.0 and dist < 10.0:
            self._lap_start_ts[num] = clock_time
            self._delta_buf[num] = []
            self._last_emit_ts.pop(num, None)

        # Eligibility gates.
        if self._cur_cls.get(num) != "PUSH":
            return  # current lap is not (or no longer) a push attempt
        cur_lap = self._cur_lap.get(num)
        if cur_lap is None or cur_lap < 2:
            return  # lap 1 = OUT lap from initial pit-out
        if num not in self._ref_dists:
            return
        lap_start = self._lap_start_ts.get(num)
        if lap_start is None:
            return
        elapsed_ms = int((clock_time - lap_start).total_seconds() * 1000)
        if elapsed_ms < MIN_LAP_ELAPSED_MS:
            return

        # Raw delta vs reference.
        t_ref = _interp(self._ref_dists[num], self._ref_times[num], dist)
        if t_ref is None:
            return
        raw_delta_s = (elapsed_ms - t_ref) / 1000.0

        # Smoothing window.
        buf = self._delta_buf.setdefault(num, [])
        buf.append((clock_time, raw_delta_s))
        cutoff = clock_time - timedelta(seconds=SMOOTHING_WINDOW_S)
        buf = [(ts, d) for ts, d in buf if ts >= cutoff]
        self._delta_buf[num] = buf

        # Throttle to one emit per EMIT_INTERVAL_S of payload time.
        last_emit = self._last_emit_ts.get(num)
        if (last_emit is not None
                and (clock_time - last_emit).total_seconds() < EMIT_INTERVAL_S):
            return
        self._last_emit_ts[num] = clock_time

        smoothed_s = statistics.median(d for _, d in buf)
        smoothed_s_rounded = round(smoothed_s, 1)
        ref_ms = self._ref_actual_ms.get(num)
        predicted_ms = (ref_ms + int(round(smoothed_s_rounded * 1000))
                        if ref_ms is not None else None)

        self._bus.emit(f"lapPrediction:{num}", {
            "lap": cur_lap,
            "distPct": round(dist, 1),
            "elapsed_ms": elapsed_ms,
            "delta_s": smoothed_s_rounded,
            "delta_raw_s": round(raw_delta_s, 2),
            "predictedTimeMs": predicted_ms,
            "referenceLapMs": ref_ms,
            "smoothingWindowN": len(buf),
        }, clock_time)
