"""
Telemetry Processor (SME design — 2026-05-31 rewrite).

Algorithm:
  * Capture ALL CarData entries and ALL position projections; no PIT/
    state gating. (Position with X=0,Y=0 is already filtered upstream by
    PositionProcessor.)
  * NumberOfLaps (TimingData) is the authoritative lap counter.
  * At each NumberOfLaps=N event, look at the car's projected dp:
      dp <  50 → message refers to the lap just started. The most recent
                  S/F crossing is lap N's start.
      dp >= 50 → message refers to the upcoming lap. The next S/F
                  crossing will be lap N's start.
  * Between two consecutive NumberOfLaps events, if more than one S/F
    crossing was detected, the LapTime reported with the new NumberOfLaps
    update (which is the just-ended lap's official time) is used to
    pick the true crossing: minimise |(C.ts - lap_prev_start) - LapTime|.
  * STOP / Retired (from TimingData) ends the driver's capture; the
    current lap is closed at the stop timestamp.
  * Only laps with N >= 1 are emitted.

Sample format on emit: [distPct, speed, rpm, gear, throttle, brake, t_ms]
where t_ms is lap-relative (= sample's absolute ms minus lap start ms).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)


WRAP_HIGH = 80.0
WRAP_LOW = 20.0
DP_SPLIT = 50.0
STALE_POSITION_S = 10.0   # if last position is older than this at a
                          # NumberOfLaps event, switch to stale-position rule.


def _parse_lap_time_ms(s):
    if not isinstance(s, str) or not s: return None
    parts = s.split(":")
    if len(parts) != 2: return None
    try:
        m, rest = parts
        sp = rest.split(".")
        ms = int(sp[1].ljust(3, "0")[:3]) if len(sp) > 1 else 0
        return int(m) * 60_000 + int(sp[0]) * 1000 + ms
    except (ValueError, IndexError):
        return None


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@dataclass
class DriverData:
    num: str
    # Chronological histories (kept since session start).
    positions: list = field(default_factory=list)         # (ts, dp)
    samples: list = field(default_factory=list)           # [dp, speed, rpm, gear, thr, brk, abs_ms]
    nlaps_events: list = field(default_factory=list)      # (ts, n)
    sf_crossings: list = field(default_factory=list)      # (ts, dp_before, dp_after)
    # State tracking.
    last_dp: Optional[float] = None
    last_position_ts: Optional[datetime] = None
    last_channels: Optional[dict] = None
    last_channels_invalid: bool = False
    pending_n_after_wrap: Optional[int] = None
    # Stale-position rule: when NumberOfLaps fires with no recent position
    # update (>10 s), the previous lap is closed at last_position_ts and the
    # new lap's boundary is parked here, set at the next position event.
    pending_n_at_next_pos: Optional[int] = None
    # STOP / tentative-close handling. When driverStatus="STOP" fires,
    # the current lap is tentatively closed at tentative_end_ts. The
    # lap is re-opened the moment a CarData sample arrives with both
    # speed > 0 AND a dp value that differs from the dp at STOP time
    # (tow movement = speed 0 does NOT count). Per SME.
    tentative_end_ts: Optional[datetime] = None
    tentative_end_lap: Optional[int] = None
    tentative_end_dp: Optional[float] = None
    # Session-start "lap 0" handling (SME 2026-06-02):
    #   If the driver's FIRST position is at dp >= 90 (= already past
    #   the pit-lane S/F but before the main S/F), those samples are
    #   "lap 0" and must be discarded. Capture (= boundary[2] anchor)
    #   begins at the FIRST position wrap. If the first dp < 90,
    #   capture begins at the first position (Case A).
    #   This flag is True while we're still in the "lap 0" zone and
    #   gets cleared the moment the first wrap occurs OR we observe a
    #   sample below 90 (= driver was actually past S/F).
    discarding_lap_0: bool = False
    seen_first_position: bool = False
    # Lap-time references (used to disambiguate spurious crossings).
    # lastlap_by_lap[N] = LapTime (ms) reported by F1 for lap N.
    lastlap_by_lap: dict = field(default_factory=dict)
    # Lap boundaries: boundaries[N] = lap N's start timestamp.
    boundaries: dict = field(default_factory=dict)        # n -> ts
    emitted: set = field(default_factory=set)
    completed_laps: dict = field(default_factory=dict)
    # Sanity (kept for log-warning behaviour).
    best_lap_time_ms: Optional[int] = None
    observed_number_of_laps: int = 0

    # ── Wrap-driven commit logic (SME 2026-06-03 v3) ──
    # Telem detects laps purely from S/F crossings (= wraps) gated by
    # checkpoint coverage. cp_seen tracks which of {25, 50, 75} the
    # driver has crossed forward since the last anchored wrap. A wrap
    # commits only when cp_seen is complete; otherwise it's spurious
    # (= pit-lane geometry, race-start dash, etc.) and triggers a reset.
    pending_start_ts: Optional[datetime] = None
    cp_seen: set = field(default_factory=set)
    committed: int = 0
    activated: bool = False
    in_pit_flag: bool = False
    last_sample_ts_for_outage: Optional[datetime] = None
    # Outage tracking: when a position-stream gap > 5s is detected, we
    # consult lap_actual_ms count at the time to figure out how many
    # laps were missed.
    OUTAGE_THRESHOLD_S: float = 5.0
    # Pre-activation tracking (drop-first-commit logic). HUL-style:
    # if the driver had position data >60s before NL=1 PitOut (= sitting
    # in pit with projection drift across pit-lane S/F), the first
    # post-activation cp-complete wrap = end of OUT lap = FIA Lap 1
    # marker timestamp = placeholder entry, not a real telemetry lap.
    first_pre_activation_pos_ts: Optional[datetime] = None
    pre_activation_window_s: float = 0.0
    drop_first_commit: bool = False


class TelemetryProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._session_type = session_type
        self._drivers: dict[str, DriverData] = {}
        # Track status history: list of (ts, status) for SC/VSC tagging.
        # Statuses of interest: "SC", "VSC", "RED", "GREEN", etc.
        self._track_status_history: list = []
        self._current_track_status: Optional[str] = None
        # Race-mode capture activation. For race sessions, telem activates
        # for ALL drivers when SessionStatus="Started" fires (= lights-out),
        # not on NL=1 (which fires at end of race lap 1, discarding the
        # race-start-to-first-S/F lap-1 driving data).
        self._race_started: bool = False
        self._race_started_ts: Optional[datetime] = None

    def subscribe(self) -> None:
        self._bus.on("CarData.z", self._handle_car_data)
        self._bus.on("TimingData", self._handle_timing_data)
        self._bus.on("position", self._handle_position)
        self._bus.on("trackStatus", self._handle_track_status)
        if self._session_type == "race":
            self._bus.on("SessionData", self._handle_session_data)
        self._bus.on("*", self._handle_wildcard)

    def _handle_session_data(self, data: Any, clock_time: datetime) -> None:
        """Race-mode: detect SessionStatus="Started" (= lights-out) and
        activate all drivers for capture. Pre-Started position samples
        are discarded (= formation lap, grid setup). Post-Started =
        race lap 1 begins and is captured."""
        if self._race_started:
            return
        if not isinstance(data, dict): return
        ss = data.get("StatusSeries")
        if not isinstance(ss, (dict, list)): return
        items = ss.values() if isinstance(ss, dict) else ss
        for entry in items:
            if not isinstance(entry, dict): continue
            if entry.get("SessionStatus") == "Started":
                self._race_started = True
                self._race_started_ts = clock_time
                # Activate all known drivers + future ones via the
                # _drv() factory (drv.activated will be set on creation
                # when self._race_started is True).
                for drv in self._drivers.values():
                    drv.activated = True
                    drv.pending_start_ts = None
                    drv.cp_seen = set()
                return

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, str): return
        if data != self._current_track_status:
            self._current_track_status = data
            self._track_status_history.append((clock_time, data))

    def _drv(self, num: str) -> DriverData:
        d = self._drivers.get(num)
        if d is None:
            d = DriverData(num=num)
            # Race mode: drivers created after race-start are already
            # activated (= lights-out has fired).
            if self._race_started:
                d.activated = True
            self._drivers[num] = d
        return d

    # ── Wildcard router ─────────────────────────────────────────────────

    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverStatus:") and isinstance(data, str):
            num = topic.split(":", 1)[1]
            if data in ("STOP", "RET"):
                self._handle_stop(num, clock_time)
        elif topic.startswith("driverLapTimes:") and isinstance(data, dict):
            # SME 2026-06-03 v3: driverLapTimes is NO LONGER the boundary
            # signal. Telem boundaries come exclusively from S/F wraps
            # (= dp 99→0 transitions with cp_seen complete). This wildcard
            # only updates `observed_number_of_laps` for placeholder-fill
            # in finalize.
            num = topic.split(":", 1)[1]
            try:
                max_key = max(int(k) for k in data)
            except ValueError:
                return
            drv = self._drv(num)
            if max_key > drv.observed_number_of_laps:
                drv.observed_number_of_laps = max_key

    # ── Position ────────────────────────────────────────────────────────

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict): return
        for num, coords in data.items():
            if not isinstance(coords, list) or len(coords) < 3: continue
            dp = coords[2]
            if not isinstance(dp, (int, float)): continue
            drv = self._drv(num)
            dp = float(dp)
            drv.positions.append((clock_time, dp))

            # Activation gate: capture starts once F1 has bumped NL to
            # 1 (= PitOut event). Pre-activation samples (= sitting in
            # pit + projection drift) are tracked only for the
            # drop-first-commit heuristic.
            if drv.activated and drv.tentative_end_ts is None:
                self._wrap_driven_commit(drv, dp, clock_time)
            elif not drv.activated:
                if drv.first_pre_activation_pos_ts is None:
                    drv.first_pre_activation_pos_ts = clock_time

            drv.last_dp = dp
            drv.last_position_ts = clock_time

    def _wrap_driven_commit(self, drv: DriverData, dp: float,
                            clock_time: datetime) -> None:
        """SME 2026-06-03 v3 telem boundary algorithm:
          - track cp_seen ({25, 50, 75} forward crossings) since the last
            anchored wrap;
          - on a wrap (dp 99→0):
              if cp_seen complete → COMMIT pending samples as the next
                lap (= drv.committed + 1, with lap 1 reserved as the
                placeholder so the 1st commit is lap 2);
              else → spurious wrap (pit-lane geometry / race-start dash);
                RESET pending and cp_seen, anchor at this wrap;
          - outage (> OUTAGE_THRESHOLD_S gap): consult lap_actual_ms (=
            via observed_number_of_laps) to determine how many laps
            crossed S/F during the gap, emit truncated + empty fills.
        """
        prev_dp = drv.last_dp

        # First position for this driver: anchor pending start.
        if drv.pending_start_ts is None:
            drv.pending_start_ts = clock_time
            drv.last_sample_ts_for_outage = clock_time
            return

        # Outage check.
        gap_s = (clock_time - drv.last_sample_ts_for_outage).total_seconds()
        drv.last_sample_ts_for_outage = clock_time
        if gap_s > drv.OUTAGE_THRESHOLD_S:
            self._handle_outage(drv, gap_s, clock_time)
            # After outage handling, pending is anchored at clock_time.
            return

        if prev_dp is None: return

        # Checkpoint forward crossing. CP crossings are smooth: at a
        # ~5Hz sampling rate the natural per-sample dp delta is ~0.2-0.5%
        # (= going from 24.x to 25.x). Pit-lane projection quirks cause
        # dp to JUMP by 80+ in a single sample (= driver stationary in
        # pit, projection switches between pit-lane endpoints). Such
        # jumps falsely populate cp_seen with {25, 50, 75} all at once,
        # leading the next pit-lane wrap to be committed as a phantom
        # lap. Only count CPs on smooth progression — at most 23.x to
        # 26.x = delta < 3%/sample per SME. Outages (> 5s gap) are
        # handled separately above.
        delta = dp - prev_dp
        if 0 < delta < 3.0:
            for cp in (25, 50, 75):
                if prev_dp < cp <= dp:
                    drv.cp_seen.add(cp)

        # Wrap detection: dp 99→0 (= S/F crossing).
        if prev_dp > WRAP_HIGH and dp < WRAP_LOW:
            drv.sf_crossings.append((clock_time, prev_dp, dp))
            if drv.cp_seen >= {25, 50, 75}:
                # Real lap end → commit.
                self._commit_pending(drv, clock_time)
            else:
                # Spurious wrap (= pit-lane geometry / race-start dash).
                # Discard pre-wrap pending. Anchor at this wrap.
                drv.pending_start_ts = clock_time
                drv.cp_seen = set()

    def _commit_pending(self, drv: DriverData, end_ts: datetime) -> None:
        """Emit lap (committed + 1)'s telemetry — no placeholder.

        1st commit = lap 1 (= lap that ended at this wrap, per F1 NL
        convention: at NL=K+1 bump, lap K just ended). Subsequent
        commits increment by 1.

        For P/Q: capture starts at NL=1 PitOut (= driver going out for
        first time). First wrap with cp_seen complete = end of lap 1.
        For race: capture starts at SessionStatus="Started" (lights-out).
        First wrap = end of race lap 1."""
        drv.committed += 1
        lap_number = drv.committed
        start_ts = drv.pending_start_ts
        if start_ts is None: start_ts = end_ts
        self._emit_lap(drv, lap_number, start_ts, end_ts)
        drv.pending_start_ts = end_ts
        drv.cp_seen = set()

    def _handle_outage(self, drv: DriverData, gap_s: float,
                       resume_ts: datetime) -> None:
        """A gap > OUTAGE_THRESHOLD_S in the position stream. Consult
        observed_number_of_laps (= driverLapTimes max key) at this
        moment to estimate how many lap completions occurred during the
        outage. Each lap-completion = one S/F crossing missed.

        Strategy: count laps "expected" (= committed+1 at outage start)
        vs "actual" (= observed_number_of_laps now). The difference is
        the count of laps the outage swallowed. Emit truncated lap +
        empty fills.
        """
        # Laps we should have committed by now per timing.
        target = drv.observed_number_of_laps
        gap_laps = target - (drv.committed + 1)  # = laps missed
        if gap_laps <= 0:
            # No laps confirmed missed. Same lap continues.
            return
        # 1) Commit the current pending as a truncated lap (= the lap
        # that was in progress when the outage started).
        # We use the moment BEFORE the gap as the truncated lap's end —
        # that's the last sample we had. Approximation: use resume_ts
        # minus the gap.
        from datetime import timedelta as _td
        truncated_end = resume_ts - _td(seconds=gap_s)
        if drv.pending_start_ts and truncated_end > drv.pending_start_ts:
            self._commit_pending(drv, truncated_end)
            gap_laps -= 1
        # 2) Emit empty placeholder laps for the rest of the gap.
        while gap_laps > 0:
            drv.committed += 1
            lap_number = drv.committed
            self._bus.emit(f"lapTelemetry:{drv.num}:{lap_number}", [], resume_ts)
            drv.completed_laps[lap_number] = {
                "started_at": resume_ts, "ended_at": resume_ts, "n_samples": 0,
                "f1_number_of_laps_at_completion": drv.observed_number_of_laps,
                "sc_vsc": None,
            }
            drv.emitted.add(lap_number)
            gap_laps -= 1
        # 3) Anchor the next pending lap at resume_ts.
        drv.pending_start_ts = resume_ts
        drv.cp_seen = set()

    # ── CarData ─────────────────────────────────────────────────────────

    def _handle_car_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict): return
        entries = data.get("Entries")
        if not isinstance(entries, list): return
        abs_ms = _epoch_ms(clock_time)
        for entry in entries:
            if not isinstance(entry, dict): continue
            cars = entry.get("Cars")
            if not isinstance(cars, dict): continue
            for num, car in cars.items():
                try:
                    if int(num) > 99: continue
                except (TypeError, ValueError): continue
                if not isinstance(car, dict): continue
                ch = car.get("Channels")
                if not isinstance(ch, dict): continue
                drv = self._drv(num)
                thr = ch.get("4", 0); brk = ch.get("5", 0)
                speed = ch.get("2", 0)
                invalid = (isinstance(thr, (int, float)) and thr > 100) \
                    or (isinstance(brk, (int, float)) and brk > 100)
                drv.last_channels = ch
                drv.last_channels_invalid = bool(invalid)
                # Can't tag a sample without a dp anchor.
                if drv.last_dp is None: continue
                # Session-start lap 0: discard pre-S/F samples (driver
                # appeared on the polyline while still in pit lane).
                if drv.discarding_lap_0: continue

                # Tentative-close re-open check (SME rule): movement is
                # speed > 0 AND dp changed from the dp captured at STOP.
                # If not yet moving, drop this sample.
                if drv.tentative_end_ts is not None:
                    moved = (isinstance(speed, (int, float)) and speed > 0
                             and drv.tentative_end_dp is not None
                             and drv.last_dp != drv.tentative_end_dp)
                    if moved:
                        drv.tentative_end_ts = None
                        drv.tentative_end_lap = None
                        drv.tentative_end_dp = None
                    else:
                        continue

                if invalid:
                    sample = [drv.last_dp, None, None, None, None, None, abs_ms]
                else:
                    sample = [
                        drv.last_dp,
                        speed, ch.get("0", 0), ch.get("3", 0),
                        thr, brk, abs_ms,
                    ]

                # Dedup: if speed AND dp both unchanged from last appended
                # sample, skip (stationary in garage / on track).
                if drv.samples:
                    last = drv.samples[-1]
                    if sample[0] == last[0] and sample[1] == last[1]:
                        continue
                drv.samples.append(sample)

    # ── TimingData ──────────────────────────────────────────────────────

    def _handle_timing_data(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict): return
        lines = data.get("Lines")
        if not isinstance(lines, dict): return
        for num, patch in lines.items():
            if not isinstance(patch, dict): continue
            drv = self._drv(num)

            # Retired → tentatively close current lap (same path as STOP).
            if patch.get("Retired") is True and drv.tentative_end_ts is None:
                self._handle_stop(num, clock_time)
                continue

            # Block timing-data processing while tentatively closed.
            # NumberOfLaps updates during tow / waiting must not advance
            # the lap counter — only real driver movement re-opens.
            if drv.tentative_end_ts is not None:
                continue

            blt = patch.get("BestLapTime")
            if isinstance(blt, dict):
                ms = _parse_lap_time_ms(blt.get("Value"))
                if ms is not None:
                    drv.best_lap_time_ms = ms

            n = patch.get("NumberOfLaps")
            ll = patch.get("LastLapTime")
            lt_ms = None
            if isinstance(ll, dict):
                lt_ms = _parse_lap_time_ms(ll.get("Value"))

            # Lap count is tracked authoritatively via driverLapTimes
            # in _handle_wildcard (= timing_processor emits the full
            # lap-times dict on each new assignment; we read its max
            # key). Counting LL field appearances HERE over-counts when
            # F1 re-sends the same LL value (e.g. post-CF summary), so
            # we don't bump observed_number_of_laps from this code path.

            if isinstance(n, int):
                # First NL bump activates the driver — for P/Q only.
                # Race mode uses SessionStatus="Started" (lights-out)
                # via _handle_session_data; NL=1 in race fires at end
                # of race lap 1 and would discard the lap-1 driving
                # data if used as the activation signal.
                if (self._session_type != "race"
                        and not drv.activated and n >= 1):
                    drv.activated = True
                    drv.pending_start_ts = None
                    drv.cp_seen = set()
                if n == 1 and drv.observed_number_of_laps < 1:
                    drv.observed_number_of_laps = 1
                last_known = drv.nlaps_events[-1][1] if drv.nlaps_events else None
                if n != last_known:
                    drv.nlaps_events.append((clock_time, n))
                    self._on_nlaps_change(drv, clock_time, n, lt_ms)
                elif lt_ms is not None:
                    # LastLap arrived without a NumberOfLaps change — pair
                    # with the lap that just ended (i.e. lap n - 1 if
                    # NumberOfLaps moved to n at the previous event).
                    if n >= 2:
                        drv.lastlap_by_lap.setdefault(n - 1, lt_ms)

    def _on_nlaps_change(self, drv: DriverData, ts: datetime, n: int,
                         lt_ms: Optional[int]) -> None:
        """NL changes drive ONLY:
          - lastlap_by_lap bookkeeping (= for any later disambiguation).
          - Red-flag rollback: NL=N re-sent after restart.

        Boundaries themselves are anchored via driverLapTimes (= LL
        assignment event) in _handle_wildcard, which gives the precise
        end-of-lap timestamp regardless of whether the LL came bundled
        with the NL bump or as a standalone (pit-lap) emit. The earlier
        outage-based boundary override has been removed because it
        misfired during long pit-sits (position stream pauses for >10s
        while the driver waits in the garage), incorrectly mapping a
        mid-pit-sit position resumption to the next lap's start.
        """
        if n < 1: return
        n_target = n + 1

        # Red-flag rule: NL re-sent after restart. We detect a re-send
        # by checking if NL=N was ALREADY in nlaps_events before this
        # call (caller appended the new (ts, n) tuple just before
        # calling us, so on a re-send n appears in events list more
        # than once).
        prior_n_events = [e for e in drv.nlaps_events[:-1] if e[1] == n]
        if prior_n_events:
            for k in [k for k in drv.boundaries if k >= n_target]:
                drv.boundaries.pop(k, None)
                drv.emitted.discard(k)
                drv.completed_laps.pop(k, None)

        if lt_ms is not None:
            drv.lastlap_by_lap[n] = lt_ms

    # ── STOP / Retired ──────────────────────────────────────────────────

    def _handle_stop(self, num: str, ts: datetime) -> None:
        """Tentatively close the current lap. The lap is re-opened by
        any subsequent CarData sample with speed > 0 AND dp != dp-at-STOP.
        If no such movement is ever observed, finalize_session emits the
        lap at tentative_end_ts."""
        drv = self._drv(num)
        if not drv.boundaries: return
        last_n = max(drv.boundaries.keys())
        if last_n in drv.emitted: return
        drv.tentative_end_ts = ts
        drv.tentative_end_lap = last_n
        drv.tentative_end_dp = drv.last_dp
        # Discard any boundary-pending wrap; post-STOP wraps must not
        # create boundaries unless movement re-opens.
        drv.pending_n_after_wrap = None

    # ── Emit ────────────────────────────────────────────────────────────

    def _maybe_emit_lap(self, drv: DriverData, n: int, end_ts: datetime) -> None:
        if n < 1 or n in drv.emitted: return
        if n not in drv.boundaries: return
        self._emit_lap(drv, n, drv.boundaries[n], end_ts)

    def _emit_lap(self, drv: DriverData, n: int,
                  start_ts: datetime, end_ts: datetime) -> None:
        if n in drv.emitted: return
        if n < 1: return
        start_ms = _epoch_ms(start_ts)
        end_ms = _epoch_ms(end_ts)
        in_lap = [s for s in drv.samples if start_ms <= s[6] < end_ms]

        # Lap-start interpolation: synthetic [0.0, ...] at start_ms if
        # this lap is a normal S/F-opened lap (= start_ts coincides with
        # a known S/F crossing).
        synth_start = self._synthetic_at_seam(drv, start_ts, dp_target=0.0)
        # Lap-end interpolation: synthetic [100.0, ...] at end_ms if
        # this lap is a normal S/F-closed lap.
        synth_end = self._synthetic_at_seam(drv, end_ts, dp_target=100.0)

        out = []
        if synth_start is not None:
            out.append([synth_start[0], synth_start[1], synth_start[2],
                        synth_start[3], synth_start[4], synth_start[5], 0])
        for s in in_lap:
            t_ms = int(s[6] - start_ms)
            out.append([s[0], s[1], s[2], s[3], s[4], s[5], t_ms])
        if synth_end is not None:
            out.append([synth_end[0], synth_end[1], synth_end[2],
                        synth_end[3], synth_end[4], synth_end[5],
                        int(end_ms - start_ms)])

        self._bus.emit(f"lapTelemetry:{drv.num}:{n}", out, end_ts)

        # SC/VSC tagging: scan trackStatus history for the lap window.
        sc_vsc = self._lap_track_status(start_ts, end_ts)
        drv.completed_laps[n] = {
            "started_at": start_ts,
            "ended_at": end_ts,
            "n_samples": len(out),
            "f1_number_of_laps_at_completion": drv.observed_number_of_laps,
            "sc_vsc": sc_vsc,
        }
        drv.emitted.add(n)

        # Emit per-driver lap-affected snapshot (full history).
        affected = {str(k): v.get("sc_vsc")
                    for k, v in drv.completed_laps.items()
                    if v.get("sc_vsc")}
        self._bus.emit(f"lapAffectedBy:{drv.num}", {
            "lap": n, "status": sc_vsc, "laps": affected,
        }, end_ts)

        # Optional sanity warning.
        dur_ms = end_ms - start_ms
        if drv.best_lap_time_ms is not None and dur_ms < drv.best_lap_time_ms - 100:
            logger.warning(
                "TelemetryProcessor: drv=%s lap=%s duration=%sms < F1 best=%sms",
                drv.num, n, dur_ms, drv.best_lap_time_ms,
            )

    def _lap_track_status(self, start_ts: datetime, end_ts: datetime):
        """Return 'SC' or 'VSC' if either was the trackStatus at any point
        during [start_ts, end_ts); else None. SC wins over VSC."""
        if not self._track_status_history:
            return None
        # Walk from the most recent status at or before start_ts forward.
        active = None
        for ts, status in self._track_status_history:
            if ts <= start_ts:
                active = status
            else:
                break
        seen_sc = active == "SC"
        seen_vsc = active == "VSC"
        for ts, status in self._track_status_history:
            if ts <= start_ts:
                continue
            if ts >= end_ts:
                break
            if status == "SC": seen_sc = True
            elif status == "VSC": seen_vsc = True
        if seen_sc: return "SC"
        if seen_vsc: return "VSC"
        return None

    def _synthetic_at_seam(self, drv: DriverData, seam_ts: datetime,
                           dp_target: float):
        """Return [dp_target, speed, rpm, gear, thr, brk] interpolated
        from the CarData samples that bracket seam_ts in time. None if
        no bracketing samples exist (e.g. seam is at session edge)."""
        seam_ms = _epoch_ms(seam_ts)
        # Find the last sample with ts <= seam_ms (bracket-before).
        before = None
        for s in reversed(drv.samples):
            if s[6] <= seam_ms:
                before = s
                break
        # Find the first sample with ts > seam_ms (bracket-after).
        after = None
        for s in drv.samples:
            if s[6] > seam_ms:
                after = s
                break
        if before is None and after is None:
            return None
        if before is None:
            return [dp_target, after[1], after[2], after[3], after[4], after[5]]
        if after is None:
            return [dp_target, before[1], before[2], before[3], before[4], before[5]]
        span = after[6] - before[6]
        if span <= 0:
            return [dp_target, before[1], before[2], before[3], before[4], before[5]]
        frac = (seam_ms - before[6]) / span
        def interp(a, b):
            if a is None or b is None: return a if a is not None else b
            return a + (b - a) * frac
        return [
            dp_target,
            interp(before[1], after[1]),
            interp(before[2], after[2]),
            interp(before[3], after[3]),
            interp(before[4], after[4]),
            interp(before[5], after[5]),
        ]

    # ── End-of-session flush ────────────────────────────────────────────

    def finalize_session(self, clock_time: datetime) -> None:
        for drv in self._drivers.values():
            self._finalize_driver(drv, clock_time)

    def _finalize_driver(self, drv: DriverData, ts: datetime) -> None:
        # In-progress lap commit: for drivers whose session ended
        # mid-lap (= last NL bump fired but no main-S/F wrap before
        # session-end / pit entry), commit the accumulated pending
        # samples as the in-progress lap. This makes telemetry of
        # lap N actually reflect what the driver was doing during
        # the N-th lap, even when lap N didn't complete cleanly.
        # End-ts = last-moving-sample timestamp (= drv.samples is
        # deduped, so stationary post-lap pit-sit time is excluded).
        max_nl_seen = max(
            (n for _, n in drv.nlaps_events), default=0
        )
        next_lap = drv.committed + 1  # next lap to commit
        if (drv.pending_start_ts is not None
                and max_nl_seen >= next_lap
                and next_lap not in drv.emitted):
            start_ms = _epoch_ms(drv.pending_start_ts)
            in_lap_samples = [s for s in drv.samples if s[6] >= start_ms]
            if in_lap_samples:
                last_sample_ms = in_lap_samples[-1][6]
                end_ts = datetime.fromtimestamp(
                    last_sample_ms / 1000.0, tz=timezone.utc)
                drv.committed += 1
                self._emit_lap(drv, next_lap, drv.pending_start_ts, end_ts)
                drv.pending_start_ts = end_ts

        # If a NumberOfLaps with dp>=50 was waiting for a wrap that
        # never came, anchor to the last position.
        if drv.pending_n_after_wrap is not None and drv.positions:
            n = drv.pending_n_after_wrap
            drv.boundaries[n] = drv.positions[-1][0]
            drv.pending_n_after_wrap = None

        # Emit any pending laps in order. The last one closes at:
        #   - tentative_end_ts (STOP happened, driver never resumed), OR
        #   - the last sample / last position timestamp otherwise.
        sorted_n = sorted(drv.boundaries.keys())
        for i, n in enumerate(sorted_n):
            if n in drv.emitted: continue
            if i + 1 < len(sorted_n):
                end_ts = drv.boundaries[sorted_n[i + 1]]
            elif (drv.tentative_end_ts is not None
                  and drv.tentative_end_lap == n):
                # STOP fired and was never re-opened by movement.
                end_ts = drv.tentative_end_ts
            else:
                if drv.samples:
                    end_ts = datetime.fromtimestamp(
                        drv.samples[-1][6] / 1000.0, tz=timezone.utc)
                elif drv.positions:
                    end_ts = drv.positions[-1][0]
                else:
                    end_ts = drv.boundaries[n]
            self._emit_lap(drv, n, drv.boundaries[n], end_ts)

        # Empty-placeholder fill (per SME rule 2: no holes in lap list).
        # Any lap 1..observed_number_of_laps that we couldn't boundary
        # (typical: GPS / position outage at session start, e.g. BOR
        # Melbourne FP1 laps 1-6) gets an empty lapTelemetry row so the
        # lap-count matches FIA's.
        max_n = drv.observed_number_of_laps
        for n in range(1, max_n + 1):
            if n in drv.emitted:
                continue
            self._bus.emit(f"lapTelemetry:{drv.num}:{n}", [], ts)
            drv.completed_laps[n] = {
                "started_at": ts, "ended_at": ts, "n_samples": 0,
                "f1_number_of_laps_at_completion": drv.observed_number_of_laps,
                "sc_vsc": None,
            }
            drv.emitted.add(n)

    # ── Persistence stubs ───────────────────────────────────────────────

    def snapshot(self): return {}
    def restore(self, state): pass

    def reset(self) -> None:
        self._drivers.clear()

    # ── External hooks (used by LapPredictionProcessor) ─────────────────

    def resolve_lap_start(self, num: str, lap_n: int):
        drv = self._drivers.get(num)
        if drv is None: return None
        rec = drv.completed_laps.get(lap_n)
        if rec is not None:
            return rec.get("started_at")
        return drv.boundaries.get(lap_n)

    def latest_sample(self, num: str):
        drv = self._drivers.get(num)
        return drv.samples[-1] if drv and drv.samples else None
