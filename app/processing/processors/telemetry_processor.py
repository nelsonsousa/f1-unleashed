"""
Telemetry Processor (SME redesign — 2026-06-10).

Pairs position samples (track-distance %) with CarData channel samples and:
  * emits a LIVE passthrough per pair — `liveTelemetry:{num}`;
  * accumulates samples per lap and emits a COMPLETED lap on lap close —
    `telemetryLap:{num}:{lap}` (persisted as a normal message row, fetched on
    demand by topic).

Pairing (D7-B, requirement-spec.md §9.3 — pipeline redesign 2026-07-28):
  Position and CarData arrive as per-entry messages, each carrying its own
  trustworthy utcTimestamp (StreamNormalizer's payload-timestamp bypass for
  .z topics — architecture-plan.md §A.3.2). Since the redesigned pipeline
  forwards messages in strict FILE order with no server-side sort (§2.3), the
  two topics' entries arrive in per-message BATCHES rather than interleaved
  (architecture-plan.md §A.7.1: a single-slot "next CarData after a
  position" rule collapses pairing yield from ~82% to ~20-26% under batched
  arrival — the D7-B regression this rewrite fixes).
  Each driver keeps a small, bounded, TIMESTAMP-ORDERED buffer of recent
  position samples. Each CarData entry is paired with the position in that
  buffer whose utcTimestamp is the nearest one AT OR BEFORE the CarData
  entry's own utcTimestamp — arrival order is irrelevant; only the two
  entries' own timestamps decide the pairing. A CarData entry with no
  eligible (preceding) position in the buffer is skipped, same as before.
  A given position MAY pair with more than one CarData entry (when several
  CarData samples fall between two position samples) — correct, since the
  car's position genuinely hasn't changed between them.

Position validity:
  * stale (dp unchanged from previous) → skip (parked car / garage);
  * backward dp jump that is NOT an S/F wrap → skip (unreliable, e.g. pit);
  * an S/F wrap (dp from high→low) is the lap boundary.
Channel validity (sample kept, channels nulled — client draws a dotted gap):
  * throttle or brake > 100;
  * speed == 0 while the position moved.

Laps:
  * lap NUMBER is authoritative from `driverLaps.currentLap` (the NoL-derived lap
    the driver is ON), NOT from LastLapTime: on currentLap K→C the just-completed
    lap is C-1. (currentLap = NoL in P/Q, NoL+1 in race, so C-1 works for both.)
  * a normal lap is bounded by S/F crossings (0%/100%, interpolated); it closes
    when currentLap increments, at the buffered closing crossing (deferred until
    that crossing arrives — position/timing arrival-order race).
  * an IN lap closes immediately at pit entry (driverStatus PIT), partial (<100%);
    an OUT lap starts at pit exit (driverStatus OUT), partial (>0%).
  * synthetic 0%/100% seam samples are interpolated only when the pre/post-S/F
    dp gap <= SYNTH_MAX_GAP_PCT (else skipped — outage);
  * activation: currentLap>=1 (first PitOut) for practice/qualifying;
    SessionStatus="Started" (lights-out) for race.
  * STOP/RET closes the in-progress lap.

Sample tuple (stored): [dp, speed, rpm, gear, throttle, brake, t_ms_rel].
"""
from __future__ import annotations

import bisect
import json
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


def _dtw_dp(obs: list, prof_s: list, prof_dp: list) -> list:
    """Plain-Python DTW (algorithm (b)): align an observed per-lap speed
    sequence to the signature speed(dp) profile; return the inferred dp for each
    observed sample. Ported from scripts.sim_lap_compare.dtw_dp (numpy → lists).
    O(n*m); callers downsample `obs` (n<=80) to keep it cheap."""
    n, m = len(obs), len(prof_s)
    INF = 1e18
    D = [[INF] * (m + 1) for _ in range(n + 1)]
    D[0][0] = 0.0
    for i in range(1, n + 1):
        oi = obs[i - 1]
        row, prow = D[i], D[i - 1]
        for j in range(1, m + 1):
            cost = abs(prof_s[j - 1] - oi)
            row[j] = cost + min(prow[j], row[j - 1], prow[j - 1])
    i, j = n, m
    acc: list = [[] for _ in range(n)]
    while i > 0 and j > 0:
        acc[i - 1].append(prof_dp[j - 1])
        diag, up, left = D[i - 1][j - 1], D[i - 1][j], D[i][j - 1]
        if diag <= up and diag <= left:
            i, j = i - 1, j - 1
        elif up <= left:
            i -= 1
        else:
            j -= 1
    out, last = [], prof_dp[0]
    for k in range(n):
        if acc[k]:
            last = sum(acc[k]) / len(acc[k])
        out.append(last)
    return out


def _interp1d(xs: list, ys: list, x: float) -> float:
    """Linear interpolation of y at x over ascending xs (clamped at the ends)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    j = bisect.bisect_right(xs, x)
    x0, x1 = xs[j - 1], xs[j]
    if x1 == x0:
        return ys[j - 1]
    y0, y1 = ys[j - 1], ys[j]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


@dataclass
class DriverData:
    num: str
    activated: bool = False
    # Pairing (D7-B): a small, timestamp-ordered buffer of recent position
    # samples — (ts_ms, dp, ts, est), ascending by ts_ms — that CarData
    # entries pair against by nearest-preceding utcTimestamp, not arrival
    # adjacency. Bounded (see _POS_BUFFER_MAX) so it can't grow unboundedly
    # across a session; old entries fall off automatically as new ones push
    # them out.
    pos_buffer: list = field(default_factory=list)
    # Fixed (fix-attempt 3, 2026-07-28): the ts_ms of the most recently
    # CONSUMED (paired) position. `_nearest_unconsumed_pos` refuses to match
    # a position at or before this watermark, restoring the "a position
    # pairs with at most one CarData entry" invariant production has always
    # enforced (`pending_pos = None` immediately after use) — see the
    # comment on `_nearest_unconsumed_pos` for why the un-consumed buffer
    # lookup alone let one stale position pair repeatedly.
    pos_watermark_ms: Optional[int] = None
    last_dp: Optional[float] = None
    last_pos_ts: Optional[datetime] = None   # ts of the previous position sample
    # Captured samples since session start:
    #   [dp, speed, rpm, gear, thr, brk, abs_ms, est]
    # est = 1 when the position was RECONSTRUCTED (estimated) during a Position.z
    # outage, else 0. Used at lap-commit to decide whether the (b) DTW matcher runs.
    samples: list = field(default_factory=list)
    # Lap tracking. Completed-lap telemetry is numbered by the AUTHORITATIVE
    # NoL counter (driverLaps.currentLap = the lap the driver is ON), NOT by
    # LastLapTime (which can be omitted). On currentLap K→C the completed lap is
    # C-1; we close it bounded by the buffered S/F crossing, deferring until the
    # closing crossing arrives (position/timing arrival-order race). An in-lap is
    # closed immediately at pit entry (driverStatus PIT), numbered by cur_lap.
    crossings: list = field(default_factory=list)  # buffered S/F crossing ts (ascending)
    pending_lap: Optional[int] = None          # completed lap awaiting its closing crossing
    pending_report_ts: Optional[datetime] = None  # when that NoL bump was reported
    live_zero_ts: Optional[datetime] = None    # current lap's S/F crossing (live elapsed zero)
    committed: int = 0                         # highest emitted lap number
    cur_lap: int = 0                           # latest driverLaps.currentLap (lap the driver is ON)
    live_lap: int = 0                          # current driving lap (live elapsed only)
    in_pit: bool = False                       # driverStatus PIT → ignore samples until OUT
    emitted: set = field(default_factory=set)
    # AC-4 (requirement-spec.md; file-impact-map.md §1 AC-4): the PIT
    # message's OWN timestamp, set when a PIT latch arrives and cleared once
    # `_maybe_close_pending_pit` actually runs the deferred close. Deferring
    # the CALL (not just its effect) is the two-call-site fix -- `_close_in_lap`
    # only fires once a locally observed CarData.z/Position.z sample's own
    # timestamp has caught up to this one, so any `.z` sample the reorder
    # buffer was still holding (up to W=1.0s) with an EARLIER own timestamp
    # is guaranteed to have already been appended to drv.samples first.
    pending_pit_close_ts: Optional[datetime] = None


class TelemetryProcessor(Processor):

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._session_type = session_type
        self._is_race = session_type == "race"
        self._drivers: dict[str, DriverData] = {}
        self._race_started = False
        # Circuit signature (loaded once from SessionInfo). matcher "b" → run the
        # DTW dp re-derivation on reconstructed laps at commit; "a"/None → never.
        self._matcher: Optional[str] = None
        self._prof_s: Optional[list] = None   # signature speed profile (per dp step)
        self._prof_dp: Optional[list] = None  # dp value at each profile index

    def subscribe(self) -> None:
        self._bus.on("CarData.z", self._handle_car_data)
        self._bus.on("position", self._handle_position)
        self._bus.on("SessionInfo", self._handle_session_info_sig)
        if self._is_race:
            self._bus.on("SessionData", self._handle_session_data)
        self._bus.on("*", self._handle_wildcard)

    # ── Circuit signature (DTW matcher + speed profile) ──────────────────────
    def _handle_session_info_sig(self, data: Any, clock_time: datetime) -> None:
        """Load the circuit signature ONCE at session start: the matcher flag and
        speed profile that drive the (b) DTW dp re-derivation. Any missing
        file/key leaves matcher "a" (no DTW), and real-position laps are never
        touched regardless."""
        if self._matcher is not None:
            return
        self._matcher = "a"   # default: no DTW unless the signature selects it
        try:
            if not isinstance(data, dict):
                return
            meeting = data.get("Meeting")
            location = meeting.get("Location") if isinstance(meeting, dict) else None
            if not location:
                return
            from app.config import DATA_DIR
            from app.processing.track_geometry import find_svg_path
            svg = find_svg_path(location)
            if svg is None:
                return
            sig_path = DATA_DIR / "analysis" / "circuit_signatures" / f"{svg.stem}.json"
            if not sig_path.exists():
                return
            with open(sig_path) as fh:
                j = json.load(fh)
            matcher = j.get("matcher")
            profile = j.get("profile")
            step = j.get("profile_step")
            if (matcher not in ("a", "b") or not isinstance(profile, list)
                    or not profile or not isinstance(step, (int, float))):
                return
            self._matcher = matcher
            self._prof_s = [float(v) for v in profile]
            self._prof_dp = [i * float(step) for i in range(len(profile))]
        except Exception:
            logger.debug("telemetry: circuit signature load failed", exc_info=True)

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
                        drv.cur_lap = 1               # on lap 1 at lights-out
                return

    # ── Wildcard: driverLaps (lap numbers) + driverStatus ──────
    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("driverLaps:"):
            self._handle_driver_laps(topic.split(":", 1)[1], data, clock_time)
        elif topic.startswith("driverStatus:") and isinstance(data, str):
            num = topic.split(":", 1)[1]
            drv = self._drv(num)
            if data == "PIT":
                # The IN lap ENDS here: partial (ends <100%), numbered by
                # cur_lap (the lap being driven). AC-4: the ACTUAL close
                # (`_close_in_lap`, which snapshots drv.samples by timestamp
                # window) is deferred — see `pending_pit_close_ts`'s
                # docstring — rather than run synchronously here. `in_pit`
                # itself still flips immediately (unchanged from before;
                # only the window-snapshotting close call is deferred, per
                # file-impact-map.md §1 AC-4's "two call site" fix scope).
                if not drv.in_pit:
                    drv.in_pit = True
                    # Deliberately NOT triggered here with PIT's own
                    # clock_time — PIT arrives on the generic (non-.z)
                    # clock, which is not itself a locally-observed .z
                    # sample and can run ahead of the .z reorder buffer's
                    # watermark by up to W. Only an ACTUAL CarData.z/
                    # Position.z arrival (`_handle_car_data`/
                    # `_handle_position`, below) may trigger the close.
                    drv.pending_pit_close_ts = clock_time
            elif data == "OUT":
                # Pit exit: the out-lap starts here — partial, starts >0%. Its
                # NUMBER is assigned when it closes (at the next currentLap bump),
                # so the OUT/NoL arrival order at the pit exit no longer matters.
                # Re-seed last_dp so the first post-pit sample doesn't read the
                # pre-pit→pit-exit dp drop as a false S/F wrap.
                drv.in_pit = False
                drv.crossings = [clock_time]
                drv.live_zero_ts = clock_time
                drv.live_lap += 1
                drv.last_dp = None
            else:
                drv.in_pit = False
            if data in ("STOP", "RET"):
                self._handle_stop(num, clock_time)

    def _handle_driver_laps(self, num: str, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        drv = self._drv(num)
        cur = data.get("currentLap")
        if not isinstance(cur, int):
            return
        # Practice/qualifying activation: first lap started (PitOut).
        if not self._is_race and not drv.activated and cur >= 1:
            drv.activated = True
            drv.crossings = [clock_time]   # pit-exit = lap 1's start boundary
            drv.live_zero_ts = clock_time
            drv.live_lap = 1
            drv.cur_lap = cur
            return
        # NoL-driven close: on currentLap K→C the completed lap is C-1, bounded
        # by its S/F crossing. (currentLap is the lap the driver is ON, so the
        # just-finished lap is one less — true for both P/Q and race.)
        if cur > drv.cur_lap:
            # Seed the live lap from the authoritative NoL the first time we see it in a race:
            # the SessionData "Started" activation can be dispatched before a driver's first
            # CarData (shared gate-open timestamp), in which case _drv() activates the car but
            # leaves live_lap/cur_lap at 0 → the live lap reads one behind all session. currentLap
            # is 1 at lights-out, so seeding from it fixes the "Lap 0 at lights-out" off-by-one.
            if self._is_race and drv.live_lap == 0 and cur >= 1:
                drv.live_lap = cur
            drv.cur_lap = cur
            if not drv.activated:
                return
            m = cur - 1
            if m > drv.committed:
                self._try_close(drv, m, clock_time)

    def _maybe_close_pending_pit(self, drv: DriverData, observed_z_ts: datetime) -> None:
        """AC-4: run the deferred PIT close once a locally observed
        CarData.z/Position.z sample's own timestamp (`observed_z_ts`) has
        caught up to the PIT message's own timestamp
        (`drv.pending_pit_close_ts`). Called both at PIT-arrival time
        (handles the common case where nothing was buffered/reordered) and
        from `_handle_car_data`/`_handle_position` on every subsequent `.z`
        arrival, so the close fires as soon as it is safe — no added
        latency beyond whatever the reorder buffer was already going to
        release anyway."""
        if drv.pending_pit_close_ts is None:
            return
        if observed_z_ts < drv.pending_pit_close_ts:
            return
        end_ts = drv.pending_pit_close_ts
        drv.pending_pit_close_ts = None
        self._close_in_lap(drv, end_ts)

    def _close_in_lap(self, drv: DriverData, end_ts: datetime) -> None:
        """Close the in-lap immediately at pit entry, numbered by cur_lap (the
        lap being driven). Partial — ends before 100% (no S/F crossing)."""
        n = drv.cur_lap
        start = drv.crossings[-1] if drv.crossings else None
        if start is not None and n > drv.committed:
            self._emit_lap(drv, n, start, end_ts)
            drv.committed = n
        drv.crossings = []          # no open lap until OUT
        drv.pending_lap = None

    # Lap m's S/F crossing lands within ~this of the lastLap report (timing can
    # report a completed lap up to ~tens of seconds AFTER the crossing — RUS FP1
    # lagged 35s). The PREVIOUS crossing is a full lap (≥65s at Melbourne)
    # earlier, so 60s cleanly separates "the closing crossing has arrived (use
    # it)" from "still pending (defer)". Too small → a lagged report defers and
    # closes against the NEXT crossing, cascading into empty + double laps.
    _CLOSE_TOL = timedelta(seconds=60)
    # Bracket kept before the open lap's start when pruning committed samples —
    # enough for _synthetic_at_seam's pre-seam sample (~18 samples at 3.7/s).
    _SAMPLE_BRACKET_MS = 5000

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
        # Samples before the open lap are now committed to telemetryLap rows and
        # never read again — drop them so DriverData.samples stays bounded and the
        # per-commit filters don't re-walk the whole session (4tgWbUfx).
        self._prune_samples(drv)

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
            # Estimated (reconstructed) position marker: position_processor emits a
            # 4th element = 1 for outage-reconstructed positions ([x, y, dp, 1]);
            # real positions are length-3. Carried into the sample for lap-commit.
            est = 1 if (len(coords) >= 4 and coords[3]) else 0
            drv = self._drv(num)
            # AC-4: a Position.z arrival also counts as "locally observed a
            # .z-derived sample" for the deferred PIT close — it doesn't
            # itself add anything to drv.samples, but since the reorder
            # buffer's watermark is shared across CarData.z/Position.z, its
            # own timestamp having caught up guarantees no earlier-timestamped
            # CarData is still pending upstream either.
            if drv.pending_pit_close_ts is not None:
                self._maybe_close_pending_pit(drv, clock_time)
            if not drv.activated:
                # Pre-race (before lights-out): pair positions with CarData so LIVE telemetry passes
                # through (dashboard gauges) — but run NO S/F/lap logic and store no samples; there
                # are no laps to persist yet. (SME 2026-07-15)
                self._push_pos(drv, dp, clock_time, est)
                drv.last_dp = dp
                continue
            if drv.in_pit:
                # Through the pit lane/garage we DO capture telemetry (so the stationary
                # speed==0 stretch is identifiable) but run NO S/F crossing detection —
                # pit-lane dp wraps are spurious projections that must not count as laps.
                # Buffered even when dp is stale (a stopped car's dp is constant) so
                # CarData can pair and emit the speed==0 samples. OUT re-seeds last_dp.
                self._push_pos(drv, dp, clock_time, est)
                drv.last_dp = dp
                drv.last_pos_ts = clock_time
                continue
            prev = drv.last_dp
            if prev is None:
                self._push_pos(drv, dp, clock_time, est)
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
                self._push_pos(drv, dp, clock_time, est)
                drv.last_dp = dp
                drv.last_pos_ts = clock_time
            elif dp > prev:
                self._push_pos(drv, dp, clock_time, est)
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

    # ── Pairing buffer (D7-B) ────────────────────────────────────────────
    # Bounded so a driver's pos_buffer can't grow across a whole session —
    # positions arrive at ~3-4 Hz, so this comfortably covers any single
    # batch (architecture-plan.md §A.7.1 observed 3-5 entries/message) with
    # wide headroom, while old entries fall off as new ones push past it.
    _POS_BUFFER_MAX = 64

    def _push_pos(self, drv: DriverData, dp: float, ts: datetime, est: int) -> None:
        ts_ms = _epoch_ms(ts)
        bisect.insort(drv.pos_buffer, (ts_ms, dp, ts, est), key=lambda e: e[0])
        if len(drv.pos_buffer) > self._POS_BUFFER_MAX:
            del drv.pos_buffer[: len(drv.pos_buffer) - self._POS_BUFFER_MAX]

    @staticmethod
    def _nearest_unconsumed_pos(drv: DriverData, car_ts_ms: int) -> Optional[tuple]:
        """The position in drv.pos_buffer with the latest ts_ms <= car_ts_ms
        (D7-B: pair by timestamp, not arrival adjacency) THAT HAS NOT ALREADY
        BEEN PAIRED to an earlier CarData entry.

        Fixed (fix-attempt 3, 2026-07-28): the original D7-B implementation
        never consumed a matched position, on the stated reasoning that "the
        car's position genuinely hasn't changed" between several CarData
        samples that fall between the same two positions — true of the DP
        VALUE, but wrong for what actually got persisted. `drv.samples`
        (the completed-lap trace: `docs/artifacts/2026-07-28-016-telemetry-
        downstream-reconciliation/data-investigation.md`) ended up storing
        several rows with byte-identical `[dp, ..., t_ms]`, differing only in
        channel readings — i.e. several genuinely distinct CarData samples
        collapsed onto a single point in the plotted trace, instead of
        spreading across it. Measured against real Spa 2026 Qualifying data:
        69.4% of committed telemetryLap samples carried a duplicated
        timestamp (production: 0.02%), and DISTINCT telemetry points per car
        collapsed to ~37.7% of production's (`distinct new/old` ratio, flat
        across all 22 cars — a single deterministic mechanism, not a race).

        Production's pre-redesign single-slot model never had this problem
        because it explicitly clears `pending_pos` immediately after a
        CarData entry consumes it (`pending_pos = None`) — a position can be
        used AT MOST ONCE. This restores that same invariant on top of D7-B's
        buffer (so batched/out-of-arrival-order delivery still finds the
        CORRECT position by timestamp, not just "whatever arrived most
        recently" — the actual thing D7-B fixed): `pos_watermark_ms` tracks
        the ts_ms of the last position actually paired; a position at or
        before that watermark is refused even if it is still the nearest
        preceding one in the buffer, so a CarData entry with no genuinely
        NEW eligible position is skipped — exactly as production always
        skipped the 2nd+ CarData in a C-run (`pending_pos is None`)."""
        buf = drv.pos_buffer
        if not buf:
            return None
        idx = bisect.bisect_right(buf, car_ts_ms, key=lambda e: e[0]) - 1
        if idx < 0:
            return None
        ts_ms, dp, ts, est = buf[idx]
        if drv.pos_watermark_ms is not None and ts_ms <= drv.pos_watermark_ms:
            return None   # nearest preceding position already consumed — no new one to pair
        return dp, ts, est, ts_ms

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
                # D7-B: pair by nearest-preceding utcTimestamp, not arrival
                # adjacency — AND, fixed fix-attempt 3, only against a
                # position not already consumed by an earlier CarData entry
                # (see `_nearest_unconsumed_pos`'s docstring for why the
                # un-consumed lookup alone caused a real 62% loss of distinct
                # telemetry resolution). `clock_time` is THIS CarData entry's
                # own utcTimestamp (StreamNormalizer's .z payload-timestamp
                # bypass — the entry's own Utc, not the message envelope).
                match = self._nearest_unconsumed_pos(drv, _epoch_ms(clock_time))
                if match is None:
                    # AC-3 (requirement-spec.md): emit UNPAIRED instead of
                    # silently skipping. dp=None, channel data still
                    # persisted — "channels present, position unknown" is a
                    # legitimate row (file-impact-map.md §3), not a dropped
                    # sample. abs_ms anchors on the CarData entry's OWN
                    # timestamp (no matched position to anchor on).
                    dp, pos_ts, est, pos_ts_ms = None, None, 0, None
                else:
                    dp, pos_ts, est, pos_ts_ms = match
                    drv.pos_watermark_ms = pos_ts_ms   # consume it — the next CarData needs a NEWER position
                # abs_ms anchors the STORED per-lap sample (drv.samples / the
                # persisted telemetryLap trace) to the matched position's own
                # timestamp — dp describes where the car was AT that position
                # sample, and lap-boundary math (crossings, _prune_samples,
                # _synthetic_at_seam) is itself derived from position-wrap
                # timestamps, so this keeps both on the same clock. car_ms is
                # this CarData entry's OWN trustworthy utcTimestamp — used
                # for the LIVE-only emission below (a live gauge should read
                # as of its own sample's arrival, not lag behind by the age
                # of the matched position; also keeps `liveTelemetry`'s
                # persisted offset_ms unique per sample regardless of the
                # (now rare, single-use) position-reuse case).
                # AC-3: no matched position (pos_ts is None) -> anchor on the
                # CarData entry's OWN timestamp instead (there is no
                # position sample to anchor on).
                abs_ms = _epoch_ms(pos_ts) if pos_ts is not None else _epoch_ms(clock_time)
                car_ms = _epoch_ms(clock_time)
                # Absent channel -> None (client draws a dotted gap), NEVER a
                # fake 0 — a default 0 was read as a real "speed dropped to 0"
                # sample. A PRESENT 0 is legitimate and kept as 0. (mrHzxVmb / L9)
                speed = ch.get("2")
                thr = ch.get("4")
                brk = ch.get("5")
                gear = ch.get("3")
                # The ECU reports REVERSE as an out-of-range gear value (>8) → map to -1 = R. (SME 2026-07-15)
                if isinstance(gear, (int, float)) and gear > 8:
                    gear = -1
                # speed==0 is a channel dropout ON TRACK (position moving) → nulled; but in
                # the pit it is a REAL standstill → kept, so stationary time is measurable.
                invalid = (
                    (isinstance(thr, (int, float)) and thr > 100)
                    or (isinstance(brk, (int, float)) and brk > 100)
                    or (speed == 0 and not drv.in_pit)
                )
                if invalid:
                    # R at a standstill is real state (reverse selected while stopped), not a
                    # dropout — keep the gear so the dash can show R. (SME 2026-07-15)
                    g = -1 if gear == -1 else None
                    sample = [dp, None, None, g, None, None, abs_ms, est]
                    live = {"dp": dp, "speed": None, "rpm": None, "gear": g,
                            "throttle": None, "brake": None}
                else:
                    sample = [dp, speed, ch.get("0"), gear, thr, brk, abs_ms, est]
                    live = {"dp": dp, "speed": speed, "rpm": ch.get("0"),
                            "gear": gear, "throttle": thr, "brake": brk}
                if drv.activated:
                    drv.samples.append(sample)   # store only once running — nothing to persist pre-race
                # AC-4: this CarData entry's own timestamp has now been
                # locally observed — if a PIT close is still pending and
                # this sample's timestamp has caught up to it, run the
                # deferred close now (drv.samples already has this sample
                # appended above, so it's included if it belongs in-window).
                if drv.pending_pit_close_ts is not None:
                    self._maybe_close_pending_pit(drv, clock_time)
                # LIVE-only fields and the emit's persistence clock use the
                # CarData entry's own timestamp (car_ms/clock_time), not the
                # (possibly reused) matched position's — see the comment on
                # `car_ms` above.
                live["ts"] = car_ms
                live["lap"] = drv.live_lap
                live["lapElapsedMs"] = (car_ms - _epoch_ms(drv.live_zero_ts)
                                        if drv.live_zero_ts is not None else None)
                self._bus.emit(f"liveTelemetry:{num}", live, clock_time)

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


    def _prune_samples(self, drv: DriverData) -> None:
        """Drop samples older than the open lap's start (minus a small bracket for
        _synthetic_at_seam's pre-seam sample). Everything before the current open
        lap is already committed to telemetryLap rows and never read again, so this
        keeps DriverData.samples memory and the per-commit filters bounded to the
        open lap rather than the whole session (4tgWbUfx)."""
        if not drv.crossings:
            return
        cutoff = _epoch_ms(drv.crossings[0]) - self._SAMPLE_BRACKET_MS
        if drv.samples and drv.samples[0][6] < cutoff:
            drv.samples = [s for s in drv.samples if s[6] >= cutoff]

    # ── Emit ──────────────────────────────────────────────────────────────
    def _emit_lap(self, drv: DriverData, n: int,
                  start_ts: datetime, end_ts: datetime) -> None:
        if n < 1 or n in drv.emitted:
            return
        start_ms = _epoch_ms(start_ts)
        end_ms = _epoch_ms(end_ts)
        in_lap = [s for s in drv.samples if start_ms <= s[6] < end_ms]

        # P/Q: never commit the stationary pit samples (speed==0) to the saved lap — a
        # parked car would flat-line the trace. On-track dropouts are speed=None (not 0),
        # so this targets only genuine in-box standstill. (Race keeps them: a race pit lap
        # legitimately shows the stop.)
        if not self._is_race:
            in_lap = [s for s in in_lap if s[1] != 0]

        # A lap's samples ascend dp 0→100. A boundary that lands a hair before
        # the S/F line leaves a pre-S/F straggler (dp~100) at the front, or a
        # next-lap straggler (dp~0) at the back — either draws a line straight
        # across the chart. Strip leading/trailing samples that jump backward
        # across the seam so the trace is monotonic in dp.
        # AC-3: a None dp (emit-unpaired, file-impact-map.md §3) can't
        # participate in this ordering comparison — None is not orderable
        # against a float. Rather than halting the whole strip on the FIRST
        # null-dp row encountered (which, at the measured 17.7-19.9% null
        # rate, left the previous-lap straggler this loop exists to remove
        # in the trace ~18-20% of the time — should-fix 4, 2026-07-29
        # fix-attempt 2), the comparison is evaluated over the NON-NULL
        # subsequence: a genuine straggler is identified and popped by its
        # own index even when a null-dp row sits between it and the sample
        # it's compared against. Null-dp rows themselves are never popped
        # here (a null dp can neither confirm nor rule out being part of the
        # stragglered run) — they stay in `in_lap`'s output as legitimate
        # "channels present, position unknown" gap samples, exactly as
        # before.
        while True:
            idxs = [k for k, s in enumerate(in_lap) if s[0] is not None]
            if len(idxs) < 2:
                break
            i0, i1 = idxs[0], idxs[1]
            if in_lap[i0][0] > in_lap[i1][0]:
                in_lap.pop(i0)
            else:
                break
        while True:
            idxs = [k for k, s in enumerate(in_lap) if s[0] is not None]
            if len(idxs) < 2:
                break
            i_last, i_prev = idxs[-1], idxs[-2]
            if in_lap[i_last][0] < in_lap[i_prev][0]:
                in_lap.pop(i_last)
            else:
                break

        out = []
        synth_start = self._synthetic_at_seam(drv, start_ts, 0.0)
        if synth_start is not None:
            out.append(synth_start[:6] + [0])
        for s in in_lap:
            out.append([s[0], s[1], s[2], s[3], s[4], s[5], int(s[6] - start_ms)])
        synth_end = self._synthetic_at_seam(drv, end_ts, 100.0)
        if synth_end is not None:
            out.append(synth_end[:6] + [int(end_ms - start_ms)])

        # (b) DTW at lap-commit: on circuits whose signature selects the DTW
        # matcher, re-derive dp for a RECONSTRUCTED lap by aligning its speed
        # trace to the signature speed profile. Real-position laps (est=0) and
        # matcher-"a" circuits are left untouched; any failure keeps the un-DTW'd
        # dp. Majority test on the lap's own samples (8th element = est).
        if self._matcher == "b" and self._prof_s:
            est_n = sum(1 for s in in_lap if len(s) > 7 and s[7] == 1)
            if est_n * 2 > len(in_lap):
                try:
                    self._dtw_relabel_dp(out, synth_start is not None,
                                         synth_end is not None)
                except Exception:
                    logger.debug("telemetry: DTW dp relabel failed", exc_info=True)

        self._bus.emit(f"telemetryLap:{drv.num}:{n}", out, end_ts)
        drv.emitted.add(n)

    def _dtw_relabel_dp(self, out: list, has_seam_start: bool,
                        has_seam_end: bool) -> None:
        """Overwrite dp (row[0]) on the non-seam rows of a reconstructed lap using
        a DTW alignment of the lap speed trace to the signature speed profile.
        The interpolated 0.0/100.0 S/F seam rows anchor the lap ends and are left
        as-is; nulled (None-speed) rows keep their original dp."""
        lo = 1 if has_seam_start else 0
        hi = len(out) - (1 if has_seam_end else 0)
        # Body rows carrying a numeric speed — the only ones we can DTW/relabel.
        idxs = [k for k in range(lo, hi) if out[k][1] is not None]
        if len(idxs) < 2:
            return
        speeds = [out[k][1] for k in idxs]
        # Downsample to <=80 evenly-spaced points to keep DTW (O(n*m)) cheap.
        MAXP = 80
        if len(speeds) > MAXP:
            sel: list = []
            seen: set = set()
            for t in range(MAXP):
                p = int(round(t * (len(speeds) - 1) / (MAXP - 1)))
                if p not in seen:
                    seen.add(p)
                    sel.append(p)
        else:
            sel = list(range(len(speeds)))
        ds_speeds = [speeds[p] for p in sel]
        ds_dp = _dtw_dp(ds_speeds, self._prof_s, self._prof_dp)
        # Map the DTW dp (defined at downsample positions `sel`, an axis over
        # 0..len(idxs)-1) back onto every numeric body row by linear interpolation.
        for pos, k in enumerate(idxs):
            out[k][0] = round(_interp1d(sel, ds_dp, pos), 3)

    def _synthetic_at_seam(self, drv: DriverData, seam_ts: datetime,
                           dp_target: float):
        """Interpolate [dp_target, speed, rpm, gear, thr, brk] from the samples
        bracketing seam_ts — only if both bracket samples exist AND their
        across-S/F dp gap <= SYNTH_MAX_GAP_PCT. Else None."""
        seam_ms = _epoch_ms(seam_ts)
        # The crossing sample (ts == seam_ms, dp ~0) belongs AFTER the seam, so
        # `before` must be strictly pre-seam (the pre-S/F dp ~99) — else the
        # across-S/F gap reads ~100% and interpolation is wrongly skipped.
        # AC-3: a None-dp (emit-unpaired) sample can't bracket a seam — the
        # gap arithmetic below needs a real dp on both sides — so it is
        # excluded from bracket selection here (it still exists in
        # drv.samples/the emitted trace, just isn't usable as a seam
        # bracket).
        before = next((s for s in reversed(drv.samples) if s[6] < seam_ms and s[0] is not None), None)
        after = next((s for s in drv.samples if s[6] >= seam_ms and s[0] is not None), None)
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
                    and n <= drv.cur_lap):
                start_ms = _epoch_ms(start)
                in_lap = [s for s in drv.samples if s[6] >= start_ms]
                if in_lap:
                    end_ts = _ms_to_dt(in_lap[-1][6])
                    self._emit_lap(drv, n, start, end_ts)
