"""
Position Processor — car positions projected onto track geometry.

Subscribes to: Position.z, CarData.z, SessionInfo
Emits:
  - trackGeometry   (corners and sectors as % of lap distance) — once, persisted
  - position        { num: [x, y, distPct] } on each Position.z change

Loads the track SVG on SessionInfo to build the track polyline, then
projects each car's X,Y onto it to compute distance as % of lap. Each emit is a
full snapshot of the cars that moved; skips messages where no car has moved.

Position outage recovery: F1's Position feed can drop out (e.g. Monaco 2026 lost
it for most of the race) while CarData (speed) keeps flowing. We tolerate up to
EST_THRESHOLD_S seconds of elapsed wall-clock time (measured from the .z messages'
own timestamps, never system time) with no new real Position — integrating
speed·Δt through them from the last real fix — and only then start dead-reckoning:
snap the accumulated drift to known corner locations (speed minima line up with
corners), map that distance back to (x, y) via the track polyline, and emit it on the
SAME `position` topic. Any real Position resets the elapsed-time clock and takes over
immediately; brief gaps (<= EST_THRESHOLD_S) are never reconstructed, so the estimate
can't fight the real feed. This is a time-based threshold, not a CarData sample count
(WB2, requirement-spec.md AC-6) -- the two are not equivalent, since CarData's own
arrival rate is not constant across sessions.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

from app.config import DATA_DIR
from app.processing.dp_reckoner import DpReckoner
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor
from app.processing.track_geometry import (
    TrackGeometry, find_svg_path, parse_svg,
    project_local, cum_dist_to_track_dist,
)

logger = logging.getLogger(__name__)

SIG_DIR = DATA_DIR / "analysis" / "circuit_signatures"   # FP1-learned apex signatures

EST_THRESHOLD_S = 1.0     # AC-6 (requirement-spec.md, WB2): wall-clock seconds since a car's last
                          # REAL Position.z fix before the `position` topic switches from
                          # tolerating (buffering) to emitting the estimated/reckoned position.
                          # Measured from message timestamps (`clock_time`), never system time --
                          # see `_is_estimating`. Deliberately NOT a CarData sample count (that was
                          # the pre-WB2 `MISS_SAMPLES` gate) -- 10 samples at the ~240ms median
                          # CarData rate is ~2.4s, and the rate is not constant across sessions.
APEX_PROM = 15.0          # a speed-minimum is only an apex if speed dropped >= this (km/h) from the peak
SNAP_TOL_PCT = 1.5        # snap reconstructed dp to an anchor only within this drift (% lap) [(a)]
APEX_SPEED_MARGIN = 0.20  # a detected minimum can only be an apex if its speed <= anchor*(1+this) [(a)]
MAX_DT_S = 2.0            # clamp per-step integration (guards against feed pauses)
GLITCH_MAX_ZEROS = 2      # up to this many consecutive speed=0 samples = glitch → carry speed


class PositionProcessor(Processor):
    """Projects car positions onto track and emits distance percentages."""

    def __init__(self, bus: SessionMessageBus, session_type: str):
        super().__init__(bus, session_type)
        self._geo: Optional[TrackGeometry] = None
        self._geometry_emitted = False
        self._last_seg: dict[str, int] = {}
        self._last_pos: dict[str, tuple[float, float, float]] = {}
        # Position-outage reconstruction state (per car).
        self._corner_pcts: list[float] = []           # SVG corner markers (fallback anchors)
        self._sig_apex: list[float] = []              # FP1-learned detectable apex dps (preferred)
        self._anchors: list[tuple[float, str, float]] = []   # (dp, 'apex'|'max', speed) sorted
        self._apex_i: dict[str, int] = {}             # per car: next expected anchor index
        self._last_pos_ts: dict[str, datetime] = {}   # last REAL Position.z time -- the AC-6
                                                        # anchor `_is_estimating` measures elapsed
                                                        # time from
        self._miss: dict[str, int] = {}               # WB2: diagnostic-only counter (consecutive
                                                        # telemetry samples w/o a real fix) -- no
                                                        # longer the gating variable, see AC-6
        self._cur_lap: dict[str, int] = {}            # driverLaps.currentLap (S/F-crossing anchor)
        self._sc_active: bool = False                 # SC/VSC/red → suspend apex snapping, dead-reckon only
        # AC-4: ordered (transition_ts, new_value) pairs -- lets
        # `_handle_car_data` look up what `_sc_active` WAS as of a `.z`
        # sample's OWN timestamp, instead of blindly reading the current
        # value (see `_sc_active_at`/`_handle_track_status`).
        self._sc_transitions: list[tuple[datetime, bool]] = []
        self._r_buf: dict[str, list] = {}             # tolerated est. positions, held for backfill
        self._wrapped: dict[str, bool] = {}           # dp wrapped naturally this lap (else force one at S/F)
        self._r_speed: dict[str, float] = {}          # last valid speed (km/h)
        self._r_smooth: dict[str, float] = {}         # last speed after oscillation smoothing
        self._r_zeros: dict[str, int] = {}            # consecutive speed=0 count
        self._r_prev: dict[str, float] = {}           # previous speed
        # ZigZag extremum tracker (per car): detects speed peaks (→ 'max' anchor) and troughs
        # (→ 'apex' anchor) with >= APEX_PROM prominence, alternating.
        self._r_dir: dict[str, int] = {}              # current swing: +1 rising, -1 falling
        self._r_ext: dict[str, float] = {}            # running extremum speed since the last pivot
        self._r_ext_dp: dict[str, float] = {}         # dp at that running extremum
        # Speed→distance scale (calibrated from real motion, dp% per km/h·s) and per-car
        # dead-reckoning anchor: both owned by the shared DpReckoner (WB1,
        # docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/), not this class,
        # so a future TelemetryProcessor integration (WB3) can share the SAME reckoner instead
        # of independently re-deriving it. `_r_dp`/`_r_ts`/`_C`/`_cal_n` below are thin proxies
        # onto `self._reckoner` kept so existing white-box tests
        # (`tests/regression/test_position_processor_max_dt_stall_discard_ac7.py`,
        # `tests/unit/test_position_processor_sc_active_lookup_coverage.py`) keep working
        # unmodified against the same attribute names.
        self._reckoner = DpReckoner()

    # ── WB1 compatibility properties ──────────────────────────────────────
    # Thin proxies onto `self._reckoner`'s internal state, kept ONLY so the
    # pre-existing white-box tests that seed/read `proc._r_dp`, `proc._r_ts`,
    # `proc._C`, `proc._cal_n` directly keep working unmodified now that this
    # state actually lives in `DpReckoner`. New code should call the
    # reckoner's own public methods (`current_dp`, `is_seeded`, `advance`,
    # `observe_real_position`, `set_dp`) instead of these properties.
    @property
    def _r_dp(self) -> dict[str, float]:
        return self._reckoner._dp

    @property
    def _r_ts(self) -> dict[str, datetime]:
        return self._reckoner._ts

    # `_C`/`_cal_n` are write-only (no getter): every reference across the
    # test suite and production code only ever *seeds* these via assignment
    # (`proc._C = ...`, `proc._cal_n = ...`) to prime `self._reckoner`'s
    # calibration state; nothing reads them back off `PositionProcessor`
    # itself (production code and `DpReckoner`-level tests read `_C`/`_cal_n`
    # off `self._reckoner` directly). A getter here would be dead code.
    def _set_C(self, value: Optional[float]) -> None:
        self._reckoner._C = value

    _C = property(fset=_set_C)

    def _set_cal_n(self, value: int) -> None:
        self._reckoner._cal_n = value

    _cal_n = property(fset=_set_cal_n)

    def subscribe(self) -> None:
        self._bus.on("SessionInfo", self._handle_session_info)
        self._bus.on("Position.z", self._handle_position)
        self._bus.on("CarData.z", self._handle_car_data)
        self._bus.on("TrackStatus", self._handle_track_status)   # SC/VSC → suspend apex snapping
        self._bus.on("*", self._handle_wildcard)      # driverLaps → S/F anchor

    def _handle_wildcard(self, topic: str, data: Any, clock_time: datetime) -> None:
        # A currentLap increment = the car just crossed S/F. During an outage the timing
        # loop still reports it (Position-independent), so we use it to re-anchor the
        # reconstructed distance to 0 each lap — otherwise per-lap drift accumulates.
        if not topic.startswith("driverLaps:") or not isinstance(data, dict):
            return
        num = topic.split(":", 1)[1]
        cl = data.get("currentLap")
        if isinstance(cl, int) and cl != self._cur_lap.get(num):
            self._cur_lap[num] = cl
            if num in self._r_dp:
                # Guarantee one S/F crossing per authoritative lap while reconstructing: under SC/VSC
                # the dp is clamped (no free-wrap), so if it hasn't wrapped this lap emit a synthetic
                # completing sample near 100 before the reset — the telemetry processor then sees a
                # 100→0 wrap and COUNTS the lap (even if its interior is imperfect).
                if (self._geo is not None and not self._wrapped.get(num)
                        and self._is_estimating(num, clock_time)):
                    x, y = self._dist_pct_to_xy(99.9)
                    self._bus.emit("position", {num: [round(x, 1), round(y, 1), 99.9, 1]}, clock_time)
                self._r_dp[num] = 0.0                  # snap to S/F line
                self._wrapped[num] = False
                self._r_dir.pop(num, None)             # fresh extremum tracking for the new lap
                self._r_ext.pop(num, None)
                self._r_ext_dp.pop(num, None)
                self._apex_i[num] = 0                  # next anchor = first of the lap

    def _handle_session_info(self, data: Any, clock_time: datetime) -> None:
        if self._geo is not None:
            return
        if not isinstance(data, dict):
            return
        meeting = data.get("Meeting")
        if not isinstance(meeting, dict):
            return
        location = meeting.get("Location")
        if not location:
            return

        svg_path = find_svg_path(location)
        if not svg_path:
            logger.warning(f"No track SVG found for {location}")
            return

        self._geo = parse_svg(svg_path)
        logger.info(f"Loaded track geometry for {location}: {len(self._geo.points)} points")
        # Corner distance-% (S/F-relative), used to snap reconstructed drift.
        total = self._geo.total_dist
        if total > 0:
            self._corner_pcts = sorted(
                cum_dist_to_track_dist(c["dist"], self._geo) / total * 100.0
                for c in self._geo.corners
            )
        # Preferred anchors: the FP1-learned circuit signature (only the apexes that reliably
        # produce a speed-minimum, at their true dp) — far more precise than SVG markers.
        sig_file = SIG_DIR / f"{svg_path.stem}.json"
        if sig_file.exists():
            try:
                sig = json.load(open(sig_file))
                self._sig_apex = sorted(a["dp"] for a in sig.get("apexes", []))
                # Typed anchors: apex minima + straight-peak maxima. A detected speed-minimum snaps
                # to the next 'apex'; a detected maximum snaps to the next 'max' (the maxima anchor
                # long no-braking straights where there is no apex to correct drift).
                mk = sig.get("markers")
                if mk:
                    self._anchors = sorted((m["dp"], m["type"], m["speed"]) for m in mk)
                else:
                    self._anchors = [(dp, "apex", 0.0) for dp in self._sig_apex]
                logger.info(f"Loaded circuit signature {svg_path.stem}: "
                            f"{sum(1 for a in self._anchors if a[1] == 'apex')} apexes + "
                            f"{sum(1 for a in self._anchors if a[1] == 'max')} maxima")
            except (json.JSONDecodeError, OSError, KeyError):
                self._sig_apex = []
                self._anchors = []

        if not self._geometry_emitted:
            self._emit_geometry(clock_time)
            self._geometry_emitted = True

    def _emit_geometry(self, clock_time: datetime) -> None:
        """Emit track corners and sectors as % of lap distance."""
        geo = self._geo
        total = geo.total_dist
        if total <= 0:
            return

        corners = []
        for c in geo.corners:
            corners.append({
                "number": c["label"],
                "pct": round(c["dist"] / total * 100, 2),
            })

        self._bus.emit("trackGeometry", {
            "corners": corners,
            "sectors": geo.sector_boundaries,
            "trackLength": round(total, 1),
        }, clock_time)

    def _handle_position(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict) or self._geo is None:
            return

        pos_data = data.get("Position") or data
        if not isinstance(pos_data, list) or not pos_data:
            return

        latest = pos_data[-1]
        entries = latest.get("Entries") or latest
        if not isinstance(entries, dict):
            return

        geo = self._geo
        total = geo.total_dist
        cars = {}
        changed = False

        for num, pos in entries.items():
            if not isinstance(pos, dict):
                continue
            try:
                if int(num) > 99:
                    continue
            except ValueError:
                continue

            x = pos.get("X")
            y = pos.get("Y")
            if x is None or y is None:
                continue
            if x == 0 and y == 0:
                continue

            last_seg = self._last_seg.get(num)
            cum_dist, seg_idx, _ = project_local(geo, x, y, last_seg)
            self._last_seg[num] = seg_idx

            track_dist = cum_dist_to_track_dist(cum_dist, geo)
            dist_pct = round(track_dist / total * 100, 3) if total > 0 else 0.0
            rx = round(x, 1)
            ry = round(y, 1)

            # Calibrate the speed→distance scale GLOBALLY (total dp advanced / total speed·dt),
            # so the Position-vs-CarData sampling mismatch cancels out; then re-seed the
            # reconstruction so it can take over seamlessly when Position drops out. Delegated
            # to the shared DpReckoner (WB1) — see dp_reckoner.py's own docstring.
            self._reckoner.observe_real_position(num, dist_pct, clock_time)
            self._last_pos_ts[num] = clock_time
            self._miss[num] = 0                        # real fix → no outage, reset counter
            self._r_buf.pop(num, None)                 # real fix → discard tolerated buffer

            prev = self._last_pos.get(num)
            if prev and prev == (rx, ry, dist_pct):
                cars[num] = [rx, ry, dist_pct]
                continue

            self._last_pos[num] = (rx, ry, dist_pct)
            cars[num] = [rx, ry, dist_pct]
            changed = True

        if changed and cars:
            self._bus.emit("position", cars, clock_time)

    # ── Position-outage reconstruction ────────────────────────────────────
    def _dist_pct_to_xy(self, dp: float) -> tuple[float, float]:
        """Inverse of the projection: distance % of lap -> (x, y) on the polyline."""
        geo = self._geo
        cum = (dp / 100.0 * geo.total_dist + geo.sf_offset) % geo.total_dist
        i = int(np.searchsorted(geo.seg_cum_dist, cum, side="right")) - 1
        i = max(0, min(i, len(geo.seg_len) - 1))
        seglen = geo.seg_len[i]
        frac = (cum - geo.seg_cum_dist[i]) / seglen if seglen > 0 else 0.0
        x = geo.seg_starts[i][0] + frac * geo.seg_dirs[i][0]
        y = geo.seg_starts[i][1] + frac * geo.seg_dirs[i][1]
        return float(x), float(y)

    def _clean_speed(self, num: str, speed: Any) -> float:
        """Filter isolated speed=0 glitches: a lone 0 bracketed by motion carries the
        last speed; a run of >GLITCH_MAX_ZEROS zeros is a genuine stop."""
        if isinstance(speed, (int, float)) and speed > 0:
            self._r_zeros[num] = 0
            self._r_speed[num] = float(speed)
            return float(speed)
        z = self._r_zeros.get(num, 0) + 1
        self._r_zeros[num] = z
        if z <= GLITCH_MAX_ZEROS:
            return self._r_speed.get(num, 0.0)       # glitch → carry last speed
        return 0.0                                    # sustained → really stopped

    def _smooth_speed(self, num: str, speed: float, thr: Any, brk: Any) -> float:
        """Discard physically-impossible speed blips using throttle/brake: a sharp DROP while at
        full throttle & no brake, or a sharp RISE while braking hard & off-throttle, is spurious
        telemetry — carry the last speed. Left unfiltered, such a blip reads to the ZigZag as a
        false peak+trough pair and mis-snaps the position (SME 2026-07-12)."""
        prev = self._r_smooth.get(num)
        if (prev is not None and isinstance(thr, (int, float)) and isinstance(brk, (int, float))):
            delta = speed - prev
            if delta < -40 and thr > 80 and brk < 10:        # decelerating hard on full throttle → impossible
                return prev
            if delta > 40 and brk > 60 and thr < 20:         # accelerating hard on the brakes → impossible
                return prev
        self._r_smooth[num] = speed
        return speed

    def _match_anchor(self, num: str, dp: float, kind: str, feat_speed: float) -> float | None:
        """Sequence-match a detected speed feature (a minimum → kind='apex', a maximum → kind='max')
        to the circuit signature, using its ORDERING as a fingerprint. Within a lap dp runs 0→100
        monotonically, so only anchors of the right kind AT OR AFTER the last one matched this lap
        are candidates — never backward, never wrapping across S/F. Snap to the nearest such anchor
        within tolerance; a feature near none of them is rejected as a fake, and a missed anchor is
        simply skipped. _apex_i = next allowed anchor index, reset to 0 at S/F."""
        anchors = self._anchors
        if not anchors:
            return None
        n = len(anchors)
        i = min(self._apex_i.get(num, 0), n)
        best_d, best_j = None, None
        for j in range(i, n):                          # forward only, no wrap
            a_dp, a_kind, a_speed = anchors[j]
            if a_kind != kind:
                continue
            # apex speed cap: a detected minimum cannot be this apex if it is much faster than the
            # signature apex speed — that is a mis-detected straight-line brake, not the corner.
            if kind == "apex" and a_speed > 0 and feat_speed > a_speed * (1.0 + APEX_SPEED_MARGIN):
                continue
            d = abs(dp - a_dp)
            if d <= SNAP_TOL_PCT and (best_d is None or d < best_d):
                best_d, best_j = d, j
        if best_j is None:
            return None
        self._apex_i[num] = best_j + 1                 # advance past it (skips any missed anchor)
        return anchors[best_j][0]

    def _handle_track_status(self, data: Any, clock_time: datetime) -> None:
        """SC (4) / red (5) / VSC (6,7) → suspend apex snapping (the signature doesn't hold at
        safety-car speeds; shallow straight-line brakes masquerade as apexes). On the return to
        green, re-anchor each car's next-expected apex from its current dp — which covers both an
        SC restart (dp≈0 at S/F → first apex) and a VSC lift mid-lap (dp wherever it is).

        AC-4 (requirement-spec.md; file-impact-map.md §1 AC-4): TrackStatus is
        unbuffered, but the CarData.z/Position.z samples it affects are now
        held in stream_normalizer's reorder buffer for up to W=1.0s — a
        `.z` sample whose OWN payload timestamp precedes this transition can
        still be PROCESSED after it (buffer-held-then-released). `_sc_active`
        itself is still updated immediately here (kept for any caller that
        wants "the current live state"), but `_handle_car_data`'s READ of it
        is deferred to `_sc_active_at(clock_time)` — a point-in-time lookup
        against `_sc_transitions`, the same self-synchronizing pattern
        `telemetry_processor._try_close` already uses via `_CLOSE_TOL`."""
        if not isinstance(data, dict):
            return
        st = str(data.get("Status", ""))
        was = self._sc_active
        self._sc_active = st in ("4", "5", "6", "7")
        self._sc_transitions.append((clock_time, self._sc_active))
        if was and not self._sc_active:
            for num, dp in self._r_dp.items():
                self._apex_i[num] = self._first_apex_after(dp)

    def _sc_active_at(self, ts: datetime) -> bool:
        """AC-4: what `_sc_active` WAS as of a `.z` sample's own payload
        timestamp `ts`, not whatever it currently is at call time — a
        point-in-time lookup over `_sc_transitions` (ascending by
        transition timestamp, since TrackStatus messages themselves always
        arrive/are processed in order). Defaults to False (not active) if
        `ts` precedes every known transition — SC/VSC is never active before
        the session's first TrackStatus message."""
        active = False
        for transition_ts, value in self._sc_transitions:
            if transition_ts <= ts:
                active = value
            else:
                break
        return active

    def _is_estimating(self, num: str, clock_time: datetime) -> bool:
        """AC-6 (requirement-spec.md, WB2): whether `num`'s `position` output
        should currently be in ESTIMATED mode -- more than EST_THRESHOLD_S
        seconds have elapsed, as of `clock_time` (the caller's OWN message
        timestamp, never wall-clock `datetime.now()`), since `num`'s last
        REAL Position.z fix. Replaces the pre-WB2 `MISS_SAMPLES`
        sample-count gate -- this is a wall-clock comparison, not a count of
        CarData ticks received during the gap. Defaults to False (not
        estimating) if `num` has never had a real fix -- there is nothing to
        have gone stale yet."""
        last_ts = self._last_pos_ts.get(num)
        if last_ts is None:
            return False
        return (clock_time - last_ts).total_seconds() > EST_THRESHOLD_S

    def _first_apex_after(self, dp: float) -> int:
        """Index of the first anchor ahead of dp (this lap has no wrap)."""
        for j, (a, _kind, _sp) in enumerate(self._anchors):
            if a > dp:
                return j
        return len(self._anchors)

    def _handle_car_data(self, data: Any, clock_time: datetime) -> None:
        if self._geo is None or not isinstance(data, dict):
            return
        entries = data.get("Entries")
        if not isinstance(entries, list) or not entries:
            return
        cars = (entries[-1] or {}).get("Cars")
        if not isinstance(cars, dict):
            return

        recon = {}
        for num, cd in cars.items():
            last_ts = self._last_pos_ts.get(num)
            if last_ts is None or self._r_ts.get(num) is None:
                continue                              # never had a fix to seed from
            self._miss[num] = self._miss.get(num, 0) + 1    # a telemetry sample with no fresh fix
            ch = cd.get("Channels") if isinstance(cd, dict) else None
            speed = self._clean_speed(num, ch.get("2") if isinstance(ch, dict) else 0)
            thr = ch.get("4") if isinstance(ch, dict) else None      # throttle %
            brk = ch.get("5") if isinstance(ch, dict) else None      # brake %
            speed = self._smooth_speed(num, speed, thr, brk)

            # Calibration (speed·dt accumulation) and the dead-reckoning integration itself are
            # delegated to the shared DpReckoner (WB1) — see dp_reckoner.py's own docstring,
            # including the AC-7 fix (the full elapsed dt now integrates; no MAX_DT_S clamp).
            # `prev_dp` is captured BEFORE calling advance() because DpReckoner's own result is
            # already wrapped (mod 100) for the normal case — the SC/VSC branch below needs the
            # PRE-advance value to apply its own (non-wrapping) 99.9%-clamp instead.
            prev_dp = self._reckoner.current_dp(num)
            result = self._reckoner.advance(num, speed, clock_time)
            if result.dp is None:
                continue                              # stale/duplicate tick, or scale not learned yet
            ddp = result.ddp

            # AC-4: deferred, point-in-time lookup — was SC/VSC active as of
            # THIS sample's own payload timestamp, not whatever _sc_active
            # currently reads (which may already reflect a later transition
            # released out of order by the .z reorder buffer).
            if self._sc_active_at(clock_time):
                # SC/VSC: dead-reckon only, and clamp below 100 so the dp never free-wraps — the
                # single crossing per lap is placed authoritatively at the S/F reset. No apex snap.
                self._r_prev[num] = speed
                dp = min(prev_dp + ddp, 99.9)
                self._reckoner.set_dp(num, dp)        # override DpReckoner's own wrapped result
                x, y = self._dist_pct_to_xy(dp)
                entry = [round(x, 1), round(y, 1), round(dp, 3), 1]   # [3]=1 → estimated (for (b) commit)
                if not self._is_estimating(num, clock_time):          # AC-6: within EST_THRESHOLD_S, tolerate/buffer
                    self._r_buf.setdefault(num, []).append((clock_time, entry))
                else:
                    for ts, e in self._r_buf.pop(num, []):
                        self._bus.emit("position", {num: e}, ts)
                    recon[num] = entry
                continue

            if prev_dp + ddp >= 100.0:
                self._wrapped[num] = True             # natural S/F wrap this lap → no synthetic needed
            dp = result.dp                            # DpReckoner's own (prev_dp + ddp) % 100.0

            # ZigZag anchor snap: track the running extremum; when speed reverses by >= APEX_PROM a
            # pivot is confirmed at that extremum's dp — a PEAK (was rising) snaps to the next 'max'
            # anchor, a TROUGH (was falling) to the next 'apex'. Maxima anchor the long no-braking
            # straights (Spielberg) where there is no apex; minima anchor the corners. The snap is
            # offset-corrected back to where the car was at the extremum.
            d = self._r_dir.get(num, 0)
            ext = self._r_ext.get(num, speed)
            matched = feat_dp = None
            if d >= 0 and speed >= ext:                    # rising → new high
                self._r_ext[num] = speed; self._r_ext_dp[num] = dp; self._r_dir[num] = 1
            elif d <= 0 and speed <= ext:                  # falling → new low
                self._r_ext[num] = speed; self._r_ext_dp[num] = dp; self._r_dir[num] = -1
            elif d == 1 and speed <= ext - APEX_PROM:      # was rising, dropped → PEAK at ext_dp
                feat_dp = self._r_ext_dp.get(num, dp)
                matched = self._match_anchor(num, feat_dp, "max", ext)   # ext = the peak speed
                self._r_dir[num] = -1; self._r_ext[num] = speed; self._r_ext_dp[num] = dp
            elif d == -1 and speed >= ext + APEX_PROM:     # was falling, rose → TROUGH (apex) at ext_dp
                feat_dp = self._r_ext_dp.get(num, dp)
                matched = self._match_anchor(num, feat_dp, "apex", ext)  # ext = the trough speed
                self._r_dir[num] = 1; self._r_ext[num] = speed; self._r_ext_dp[num] = dp
            if matched is not None:
                corr = ((matched - feat_dp + 50.0) % 100.0) - 50.0
                dp = (dp + corr) % 100.0
            self._r_prev[num] = speed
            self._r_dp[num] = dp

            x, y = self._dist_pct_to_xy(dp)
            entry = [round(x, 1), round(y, 1), round(dp, 3), 1]   # [3]=1 → estimated (for (b) commit)
            if not self._is_estimating(num, clock_time):          # AC-6: within EST_THRESHOLD_S, tolerate/buffer
                self._r_buf.setdefault(num, []).append((clock_time, entry))   # tolerate: buffer
            else:
                for ts, e in self._r_buf.pop(num, []):
                    self._bus.emit("position", {num: e}, ts)                   # backfill the onset
                recon[num] = entry

        if recon:
            self._bus.emit("position", recon, clock_time)
