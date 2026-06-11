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
from datetime import datetime, timedelta, timezone
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
    # Lap tracking. Completed-lap telemetry is numbered by the AUTHORITATIVE
    # driverLaps.lastLap.lap and bounded by S/F crossings: we buffer crossings,
    # and when timing reports lap N complete, lap N ends at the latest crossing
    # and the preceding crossing-windows back-fill laps N, N-1, … This back-fill
    # numbers laps that never got a lastLap of their own (a no-time out lap),
    # absorbs a spurious pre-out-lap crossing, and survives the timing/position
    # arrival-order race (defer until the closing crossing arrives).
    crossings: list = field(default_factory=list)  # buffered S/F crossing ts (ascending)
    pending_lap: Optional[int] = None          # timing said this lap completed; awaiting its crossing
    pending_report_ts: Optional[datetime] = None  # when that lap-complete was reported
    live_zero_ts: Optional[datetime] = None    # current lap's S/F crossing (live elapsed zero)
    committed: int = 0                         # highest emitted lap number
    completed_target: int = 0                  # latest driverLaps.lastLap.lap
    live_lap: int = 0                          # current driving lap (live elapsed only)
    in_pit: bool = False                       # driverStatus PIT → crossings are spurious
    pit_close_pending: bool = False            # entered pit → close the in-lap at next NoL bump
    pit_entry_ts: Optional[datetime] = None    # when the driver went into the pit (in-lap end)
    in_garage: bool = False                    # in-lap closed; no telemetry until OUT
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
                        drv.crossings = [clock_time]  # lights-out = lap 1 start
                        drv.live_zero_ts = clock_time
                        drv.live_lap = 1
                return

    # ── Wildcard: driverLaps (lap numbers) + driverStatus ──────
    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._handle_driver_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:") and isinstance(data, str):
            num = topic.split(":", 1)[1]
            drv = self._drv(num)
            if data == "PIT":
                # Entering the pit: ignore in-pit S/F crossings, and mark the
                # current lap (the in-lap) to be CLOSED at the next NoL bump
                # (it has no S/F crossing — it ends at pit entry, partial).
                if not drv.in_pit:
                    drv.in_pit = True
                    drv.pit_close_pending = True
                    drv.pit_entry_ts = clock_time
            elif data == "OUT":
                # Pit exit: a new (out-)lap starts here (partial, starts >0%).
                drv.in_pit = False
                drv.in_garage = False
                drv.pit_close_pending = False
                drv.crossings = [clock_time]
                drv.live_zero_ts = clock_time
            else:
                drv.in_pit = False
            if data in ("STOP", "RET"):
                self._handle_stop(num, clock_time)

    def _handle_driver_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        drv = self._drv(num)
        cur = data.get("currentLap")
        if isinstance(cur, int):
            # Practice/qualifying activation: first lap started (PitOut).
            if not self._is_race and not drv.activated and cur >= 1:
                drv.activated = True
                drv.crossings = [clock_time]   # pit-exit = lap 1's start boundary
                drv.live_zero_ts = clock_time
                drv.live_lap = 1
        last = data.get("lastLap")
        m = last.get("lap") if isinstance(last, dict) else None
        if isinstance(m, int) and m > drv.completed_target:
            drv.completed_target = m
            if not drv.activated:
                return
            if drv.pit_close_pending:
                # The in-lap completed (NoL bumped) — close it at pit entry, no
                # S/F crossing. Then we're in the garage: no telemetry until OUT.
                self._close_in_lap(drv, m)
                drv.pit_close_pending = False
                drv.in_garage = True
                return
            if drv.in_garage:
                # Garage laps: NoL counts them but there's no telemetry. Keep
                # the lap number aligned so the out-lap closes with the right N.
                drv.committed = m
                return
            if m > drv.committed:
                self._try_close(drv, m, clock_time)

    def _close_in_lap(self, drv: DriverData, m: int) -> None:
        """Close the lap the driver took INTO the pits, bounded by pit entry
        (no S/F crossing; partial lap, ends before 100%)."""
        start = drv.crossings[-1] if drv.crossings else None
        if (start is not None and drv.pit_entry_ts is not None
                and m > drv.committed):
            self._emit_lap(drv, m, start, drv.pit_entry_ts)
        drv.committed = m
        drv.crossings = []          # no open lap during the garage
        drv.pending_lap = None

    # Lap m's S/F crossing lands within ~this of the lastLap report (timing can
    # report a completed lap up to ~tens of seconds AFTER the crossing — RUS FP1
    # lagged 35s). The PREVIOUS crossing is a full lap (≥65s at Melbourne)
    # earlier, so 60s cleanly separates "the closing crossing has arrived (use
    # it)" from "still pending (defer)". Too small → a lagged report defers and
    # closes against the NEXT crossing, cascading into empty + double laps.
    _CLOSE_TOL = timedelta(seconds=60)

    def _try_close(self, drv: DriverData, m: int, report_ts: datetime) -> None:
        """Close completed laps up to authoritative lap m using buffered S/F
        crossings. Lap m ends at the latest crossing; preceding crossing-windows
        back-fill laps m, m-1, … (so a no-time out lap that never got its own
        lastLap still gets numbered, and a spurious pre-out-lap crossing is
        dropped). If lap m's closing crossing hasn't arrived yet — the latest
        crossing is older than the report by more than a lap — defer until it
        does (the position handler retries on the next crossing)."""
        if (not drv.crossings or len(drv.crossings) < 2
                or drv.crossings[-1] < report_ts - self._CLOSE_TOL):
            drv.pending_lap = m
            drv.pending_report_ts = report_ts
            return
        nlaps = m - drv.committed
        windows = len(drv.crossings) - 1          # crossing-bounded laps available
        n = min(nlaps, windows)                    # how many we can actually bound
        # Assign the LAST n windows to laps m, m-1, …, m-n+1.
        for j in range(n):
            lap_no = m - j
            if lap_no <= drv.committed:
                break
            self._emit_lap(drv, lap_no,
                           drv.crossings[-2 - j], drv.crossings[-1 - j])
        drv.committed = m
        drv.pending_lap = None
        drv.pending_report_ts = None
        # The latest crossing is the start of the next (open) lap; keep only it.
        drv.crossings = [drv.crossings[-1]]

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
                if not drv.in_pit:
                    # S/F crossing → buffer it. If timing already reported a lap
                    # complete (pending_lap), this is its closing crossing —
                    # close now. A wrap while in PIT is a spurious pit-lane
                    # projection and ignored.
                    line_ts = self._line_ts(prev, drv.last_pos_ts, dp, clock_time)
                    drv.crossings.append(line_ts)
                    if drv.pending_lap is not None and drv.pending_lap > drv.committed:
                        self._try_close(drv, drv.pending_lap,
                                        drv.pending_report_ts or clock_time)
                    drv.live_lap += 1
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
                self._bus.emit(f"liveTelemetry:{num}", live, pos_ts)

    # ── STOP / Retired ────────────────────────────────────────────────────
    def _handle_stop(self, num: str, ts: datetime) -> None:
        drv = self._drv(num)
        start = drv.crossings[-1] if drv.crossings else None
        if start is None:
            return
        n = drv.committed + 1
        if n in drv.emitted:
            return
        start_ms = _epoch_ms(start)
        in_lap = [s for s in drv.samples if s[6] >= start_ms]
        if not in_lap:
            return
        end_ts = _ms_to_dt(in_lap[-1][6])
        self._emit_lap(drv, n, start, end_ts)
        drv.committed = n
        drv.crossings = []


    # ── Emit ──────────────────────────────────────────────────────────────
    def _emit_lap(self, drv: DriverData, n: int,
                  start_ts: datetime, end_ts: datetime) -> None:
        if n < 1 or n in drv.emitted:
            return
        start_ms = _epoch_ms(start_ts)
        end_ms = _epoch_ms(end_ts)
        in_lap = [s for s in drv.samples if start_ms <= s[6] < end_ms]

        # A lap's samples ascend dp 0→100. A boundary that lands a hair before
        # the S/F line leaves a pre-S/F straggler (dp~100) at the front, or a
        # next-lap straggler (dp~0) at the back — either draws a line straight
        # across the chart. Strip leading/trailing samples that jump backward
        # across the seam so the trace is monotonic in dp.
        while len(in_lap) >= 2 and in_lap[0][0] > in_lap[1][0]:
            in_lap.pop(0)
        while len(in_lap) >= 2 and in_lap[-1][0] < in_lap[-2][0]:
            in_lap.pop()

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
            start = drv.crossings[-1] if drv.crossings else None
            if (start is not None and n not in drv.emitted
                    and n <= drv.completed_target + 1):
                start_ms = _epoch_ms(start)
                in_lap = [s for s in drv.samples if s[6] >= start_ms]
                if in_lap:
                    end_ts = _ms_to_dt(in_lap[-1][6])
                    self._emit_lap(drv, n, start, end_ts)
