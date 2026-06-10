"""
Telemetry Processor (SME redesign — 2026-06-10).

Pairs position samples (track-distance %) with CarData channel samples and:
  * emits a LIVE passthrough per pair — `liveTelemetry:{num}`;
  * accumulates samples per lap and emits a COMPLETED lap on lap close —
    `telemetryLap:{num}:{lap}` (persisted as a normal message row, fetched on
    demand by topic).

Pairing (strict 1:1):
  Position and CarData arrive as per-entry messages already keyed by their
  payload timestamp (file_reader splits the 1-msg/s batches). Each position
  sample is held pending and assigned to the NEXT CarData sample; the pair's
  reference timestamp is the POSITION Timestamp. A CarData with no pending
  position is skipped; a position arriving while one is already pending
  replaces it (the older is skipped).

Position validity:
  * stale (dp unchanged from previous) → skip (parked car / garage);
  * backward dp jump that is NOT an S/F wrap → skip (unreliable, e.g. pit);
  * an S/F wrap (dp from high→low) is the lap boundary.
Channel validity (sample kept, channels nulled — client draws a dotted gap):
  * throttle or brake > 100;
  * speed == 0 while the position moved.

Laps:
  * lap NUMBER is authoritative from `driverLaps` (lap_timing);
  * the boundary is the S/F crossing; lap N spans [crossing N-1, crossing N];
  * synthetic 0%/100% seam samples are interpolated only when the pre/post-S/F
    dp gap <= SYNTH_MAX_GAP_PCT (else skipped — outage);
  * a position outage simply drops samples; numbering resyncs from driverLaps.
  * activation: NumberOfLaps>=1 (driverLaps currentLap) for practice/qualifying;
    SessionStatus="Started" (lights-out) for race.
  * STOP/RET closes the in-progress lap.

Sample tuple (stored): [dp, speed, rpm, gear, throttle, brake, t_ms_rel].
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)

WRAP_HIGH = 90.0          # prev dp above this …
WRAP_LOW = 10.0           # … and next dp below this = S/F wrap
SYNTH_MAX_GAP_PCT = 5.0   # max pre/post-S/F dp gap to interpolate a seam sample


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


@dataclass
class DriverData:
    num: str
    activated: bool = False
    # Pairing.
    pending_pos: Optional[tuple] = None      # (dp, ts) awaiting next CarData
    last_dp: Optional[float] = None
    last_pos_ts: Optional[datetime] = None   # ts of the previous position sample
    # Captured samples since session start: [dp, speed, rpm, gear, thr, brk, abs_ms].
    samples: list = field(default_factory=list)
    # S/F crossing timestamps (datetimes), consumed as laps close.
    crossings: list = field(default_factory=list)
    # Lap tracking.
    lap_start_ts: Optional[datetime] = None  # start of the current open lap
    live_zero_ts: Optional[datetime] = None  # current lap's S/F crossing (live elapsed zero)
    committed: int = 0                       # highest emitted lap number
    completed_target: int = 0                # latest driverLaps.lastLap.lap
    live_lap: Optional[int] = None           # driverLaps currentLap (live)
    emitted: set = field(default_factory=set)


class TelemetryProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._session_type = session_type
        self._is_race = session_type == "race"
        self._drivers: dict[str, DriverData] = {}
        self._race_started = False

    def subscribe(self) -> None:
        self._bus.on("CarData.z", self._handle_car_data)
        self._bus.on("position", self._handle_position)
        if self._is_race:
            self._bus.on("SessionData", self._handle_session_data)
        self._bus.on("*", self._handle_wildcard)

    def _drv(self, num: str) -> DriverData:
        d = self._drivers.get(num)
        if d is None:
            d = DriverData(num=num)
            if self._race_started:
                d.activated = True
            self._drivers[num] = d
        return d

    # ── Activation (race) ────────────────────────────────────────────────
    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        if self._race_started or not isinstance(data, dict):
            return
        ss = data.get("StatusSeries")
        items = ss.values() if isinstance(ss, dict) else (ss if isinstance(ss, list) else [])
        for entry in items:
            if isinstance(entry, dict) and entry.get("SessionStatus") == "Started":
                self._race_started = True
                for drv in self._drivers.values():
                    if not drv.activated:
                        drv.activated = True
                        drv.lap_start_ts = clock_time
                        drv.live_zero_ts = clock_time
                return

    # ── Wildcard: driverLaps (lap numbers) + driverStatus (STOP/RET) ──────
    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._handle_driver_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:") and data in ("STOP", "RET"):
            self._handle_stop(topic.split(":", 1)[1], clock_time)

    def _handle_driver_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        drv = self._drv(num)
        cur = data.get("currentLap")
        if isinstance(cur, int):
            drv.live_lap = cur
            # Practice/qualifying activation: first lap started (PitOut).
            if not self._is_race and not drv.activated and cur >= 1:
                drv.activated = True
                drv.lap_start_ts = clock_time
                drv.live_zero_ts = clock_time
        last = data.get("lastLap")
        m = last.get("lap") if isinstance(last, dict) else None
        if isinstance(m, int) and m > drv.completed_target:
            drv.completed_target = m
            self._close_laps_up_to(drv, m, clock_time)

    # ── Position ──────────────────────────────────────────────────────────
    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, coords in data.items():
            if not isinstance(coords, list) or len(coords) < 3:
                continue
            dp = coords[2]
            if not isinstance(dp, (int, float)):
                continue
            dp = float(dp)
            drv = self._drv(num)
            if not drv.activated:
                drv.last_dp = dp
                continue
            prev = drv.last_dp
            if prev is None:
                drv.pending_pos = (dp, clock_time)
                drv.last_dp = dp
                drv.last_pos_ts = clock_time
                continue
            if prev > WRAP_HIGH and dp < WRAP_LOW:
                # S/F wrap → lap boundary. Use the INTERPOLATED time at the line
                # (dp 0/100) as the zero — consistent across laps regardless of
                # where the post-crossing sample lands (0.1% vs 0.8%).
                line_ts = self._line_ts(prev, drv.last_pos_ts, dp, clock_time)
                drv.crossings.append(line_ts)
                drv.live_zero_ts = line_ts
                drv.pending_pos = (dp, clock_time)
                drv.last_dp = dp
                drv.last_pos_ts = clock_time
            elif dp > prev:
                drv.pending_pos = (dp, clock_time)
                drv.last_dp = dp
                drv.last_pos_ts = clock_time
            # else: stale (dp == prev) or backward jump → skip, keep last_dp.

    @staticmethod
    def _line_ts(prev_dp: float, prev_ts: Optional[datetime],
                 dp: float, now_ts: datetime) -> datetime:
        """Interpolate the timestamp at which dp crossed the S/F line (100→0)."""
        total = (100.0 - prev_dp) + dp
        if prev_ts is None or total <= 0:
            return now_ts
        frac = (100.0 - prev_dp) / total
        return prev_ts + (now_ts - prev_ts) * frac

    # ── CarData ───────────────────────────────────────────────────────────
    def _handle_car_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        entries = data.get("Entries")
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            cars = entry.get("Cars")
            if not isinstance(cars, dict):
                continue
            for num, car in cars.items():
                try:
                    if int(num) > 99:
                        continue
                except (TypeError, ValueError):
                    continue
                if not isinstance(car, dict):
                    continue
                ch = car.get("Channels")
                if not isinstance(ch, dict):
                    continue
                drv = self._drv(num)
                if not drv.activated or drv.pending_pos is None:
                    continue   # no pending position → skip this CarData
                dp, pos_ts = drv.pending_pos
                drv.pending_pos = None
                abs_ms = _epoch_ms(pos_ts)
                speed = ch.get("2", 0)
                thr = ch.get("4", 0)
                brk = ch.get("5", 0)
                invalid = (
                    (isinstance(thr, (int, float)) and thr > 100)
                    or (isinstance(brk, (int, float)) and brk > 100)
                    or speed == 0
                )
                if invalid:
                    sample = [dp, None, None, None, None, None, abs_ms]
                    live = {"dp": dp, "speed": None, "rpm": None, "gear": None,
                            "throttle": None, "brake": None}
                else:
                    sample = [dp, speed, ch.get("0", 0), ch.get("3", 0), thr, brk, abs_ms]
                    live = {"dp": dp, "speed": speed, "rpm": ch.get("0", 0),
                            "gear": ch.get("3", 0), "throttle": thr, "brake": brk}
                drv.samples.append(sample)
                live["ts"] = abs_ms
                live["lap"] = drv.live_lap
                live["lapElapsedMs"] = (abs_ms - _epoch_ms(drv.live_zero_ts)
                                        if drv.live_zero_ts is not None else None)
                # Live-only: consumed in real time, never replayed → not persisted.
                self._bus.emit(f"liveTelemetry:{num}", live, pos_ts, persist=False)

    # ── STOP / Retired ────────────────────────────────────────────────────
    def _handle_stop(self, num: str, ts: datetime) -> None:
        drv = self._drv(num)
        if drv.lap_start_ts is None:
            return
        n = drv.committed + 1
        if n in drv.emitted:
            return
        start_ms = _epoch_ms(drv.lap_start_ts)
        in_lap = [s for s in drv.samples if s[6] >= start_ms]
        if not in_lap:
            return
        end_ts = _ms_to_dt(in_lap[-1][6])
        self._emit_lap(drv, n, drv.lap_start_ts, end_ts)
        drv.committed = n
        drv.lap_start_ts = None

    # ── Lap close ─────────────────────────────────────────────────────────
    def _close_laps_up_to(self, drv: DriverData, target_m: int,
                          ref_ts: datetime) -> None:
        while drv.committed < target_m:
            target = drv.committed + 1
            crossing = self._next_crossing(drv)
            if crossing is None:
                # Outage / no S/F crossing for this completion — skip the lap's
                # telemetry; resync the open lap roughly at the timing ref.
                drv.committed = target
                drv.lap_start_ts = ref_ts
                continue
            if drv.lap_start_ts is not None and target not in drv.emitted:
                self._emit_lap(drv, target, drv.lap_start_ts, crossing)
            drv.lap_start_ts = crossing
            drv.committed = target
            drv.crossings = [c for c in drv.crossings if c > crossing]

    def _next_crossing(self, drv: DriverData) -> Optional[datetime]:
        """Earliest unconsumed crossing after the current lap start."""
        start = drv.lap_start_ts
        cand = sorted(c for c in drv.crossings
                      if start is None or c > start)
        return cand[0] if cand else None

    # ── Emit ──────────────────────────────────────────────────────────────
    def _emit_lap(self, drv: DriverData, n: int,
                  start_ts: datetime, end_ts: datetime) -> None:
        if n < 1 or n in drv.emitted:
            return
        start_ms = _epoch_ms(start_ts)
        end_ms = _epoch_ms(end_ts)
        in_lap = [s for s in drv.samples if start_ms <= s[6] < end_ms]

        out = []
        synth_start = self._synthetic_at_seam(drv, start_ts, 0.0)
        if synth_start is not None:
            out.append(synth_start[:6] + [0])
        for s in in_lap:
            out.append([s[0], s[1], s[2], s[3], s[4], s[5], int(s[6] - start_ms)])
        synth_end = self._synthetic_at_seam(drv, end_ts, 100.0)
        if synth_end is not None:
            out.append(synth_end[:6] + [int(end_ms - start_ms)])

        self._bus.emit(f"telemetryLap:{drv.num}:{n}", out, end_ts)
        drv.emitted.add(n)

    def _synthetic_at_seam(self, drv: DriverData, seam_ts: datetime,
                           dp_target: float):
        """Interpolate [dp_target, speed, rpm, gear, thr, brk] from the samples
        bracketing seam_ts — only if both bracket samples exist AND their
        across-S/F dp gap <= SYNTH_MAX_GAP_PCT. Else None."""
        seam_ms = _epoch_ms(seam_ts)
        # The crossing sample (ts == seam_ms, dp ~0) belongs AFTER the seam, so
        # `before` must be strictly pre-seam (the pre-S/F dp ~99) — else the
        # across-S/F gap reads ~100% and interpolation is wrongly skipped.
        before = next((s for s in reversed(drv.samples) if s[6] < seam_ms), None)
        after = next((s for s in drv.samples if s[6] >= seam_ms), None)
        if before is None or after is None:
            return None
        # Across-S/F gap: before.dp is near 100, after.dp near 0.
        gap = (100.0 - before[0]) + after[0]
        if gap > SYNTH_MAX_GAP_PCT:
            return None
        span = after[6] - before[6]
        frac = (seam_ms - before[6]) / span if span > 0 else 0.0

        def interp(a, b):
            if a is None or b is None:
                return a if a is not None else b
            return a + (b - a) * frac

        return [
            dp_target,
            interp(before[1], after[1]),
            interp(before[2], after[2]),
            interp(before[3], after[3]),
            interp(before[4], after[4]),
            interp(before[5], after[5]),
        ]

    # ── End-of-session flush ──────────────────────────────────────────────
    def finalize_session(self, clock_time: datetime) -> None:
        for drv in self._drivers.values():
            n = drv.committed + 1
            if (drv.lap_start_ts is not None and n not in drv.emitted
                    and n <= drv.completed_target + 1):
                start_ms = _epoch_ms(drv.lap_start_ts)
                in_lap = [s for s in drv.samples if s[6] >= start_ms]
                if in_lap:
                    end_ts = _ms_to_dt(in_lap[-1][6])
                    self._emit_lap(drv, n, drv.lap_start_ts, end_ts)
