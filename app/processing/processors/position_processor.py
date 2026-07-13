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
MISS_SAMPLES consecutive telemetry samples with no new real Position — integrating
speed·Δt through them from the last real fix — and only then start dead-reckoning:
snap the accumulated drift to known corner locations (speed minima line up with
corners), map that distance back to (x, y) via the track polyline, and emit it on the
SAME `position` topic. Any real Position resets the counter and takes over immediately;
brief gaps are never reconstructed, so the estimate can't fight the real feed.
"""

import json
import logging
from datetime import datetime
from typing import Any, Optional

import numpy as np

from app.config import DATA_DIR
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor
from app.processing.track_geometry import (
    TrackGeometry, find_svg_path, parse_svg,
    project_local, cum_dist_to_track_dist,
)

logger = logging.getLogger(__name__)

SIG_DIR = DATA_DIR / "analysis" / "circuit_signatures"   # FP1-learned apex signatures

MISS_SAMPLES = 10         # tolerate this many telemetry samples with no real Position before estimating
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
        self._last_pos_ts: dict[str, datetime] = {}   # last REAL Position.z time
        self._miss: dict[str, int] = {}               # consecutive telemetry samples w/o a real fix
        self._cur_lap: dict[str, int] = {}            # driverLaps.currentLap (S/F-crossing anchor)
        self._sc_active: bool = False                 # SC/VSC/red → suspend apex snapping, dead-reckon only
        self._r_buf: dict[str, list] = {}             # tolerated est. positions, held for backfill
        self._r_dp: dict[str, float] = {}             # reconstructed distance %
        self._wrapped: dict[str, bool] = {}           # dp wrapped naturally this lap (else force one at S/F)
        self._r_ts: dict[str, datetime] = {}          # last integration time
        self._r_speed: dict[str, float] = {}          # last valid speed (km/h)
        self._r_smooth: dict[str, float] = {}         # last speed after oscillation smoothing
        self._r_zeros: dict[str, int] = {}            # consecutive speed=0 count
        self._r_prev: dict[str, float] = {}           # previous speed
        # ZigZag extremum tracker (per car): detects speed peaks (→ 'max' anchor) and troughs
        # (→ 'apex' anchor) with >= APEX_PROM prominence, alternating.
        self._r_dir: dict[str, int] = {}              # current swing: +1 rising, -1 falling
        self._r_ext: dict[str, float] = {}            # running extremum speed since the last pivot
        self._r_ext_dp: dict[str, float] = {}         # dp at that running extremum
        # Speed→distance scale, calibrated from real motion (dp% per km/h·s). The SVG has
        # no real lap length, so we learn it: dp advanced between two fixes / (speed·dt).
        self._C: Optional[float] = None
        self._cal_dp_sum: float = 0.0                 # Σ real dp advanced (all cars)
        self._cal_sv_sum: float = 0.0                 # Σ speed·dt over CONTINUOUS CarData (all cars)
        self._cal_n: int = 0                          # position intervals contributing
        self._cal_prev_dp: dict[str, float] = {}      # per car: last real dp
        self._cal_car_ts: dict[str, datetime] = {}    # per car: last CarData time (sv dt)

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
                        and self._miss.get(num, 0) > MISS_SAMPLES):
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
            # reconstruction so it can take over seamlessly when Position drops out.
            prev_dp = self._cal_prev_dp.get(num)
            if prev_dp is not None:
                ddp = (dist_pct - prev_dp) % 100.0
                if 0.001 < ddp < 10.0:                 # sane forward advance, no wrap/pit jump
                    self._cal_dp_sum += ddp
                    self._cal_n += 1
                    if self._cal_sv_sum > 0:
                        self._C = self._cal_dp_sum / self._cal_sv_sum
                        if self._cal_n == 400:
                            logger.info(f"[recon] calibrated C={self._C:.5f} dp%/(kph·s) "
                                        f"(~0.0083 expected for a 3.3 km lap)")
            self._cal_prev_dp[num] = dist_pct
            self._last_pos_ts[num] = clock_time
            self._miss[num] = 0                        # real fix → no outage, reset counter
            self._r_buf.pop(num, None)                 # real fix → discard tolerated buffer
            self._r_dp[num] = dist_pct
            self._r_ts[num] = clock_time

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
        SC restart (dp≈0 at S/F → first apex) and a VSC lift mid-lap (dp wherever it is)."""
        if not isinstance(data, dict):
            return
        st = str(data.get("Status", ""))
        was = self._sc_active
        self._sc_active = st in ("4", "5", "6", "7")
        if was and not self._sc_active:
            for num, dp in self._r_dp.items():
                self._apex_i[num] = self._first_apex_after(dp)

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

            # calibration: accumulate speed·dt over CONTINUOUS CarData steps (NOT reset by fixes,
            # so the accumulator covers the same span the position deltas measure).
            prev_car = self._cal_car_ts.get(num)
            self._cal_car_ts[num] = clock_time
            if prev_car is not None:
                dtc = (clock_time - prev_car).total_seconds()
                if 0 < dtc < 5:
                    self._cal_sv_sum += speed * dtc

            dt = (clock_time - self._r_ts[num]).total_seconds()
            self._r_ts[num] = clock_time
            if dt <= 0:
                continue
            dt = min(dt, MAX_DT_S)
            if self._C is None or self._cal_n < 30:
                continue                              # scale not learned yet
            ddp = self._C * speed * dt

            if self._sc_active:
                # SC/VSC: dead-reckon only, and clamp below 100 so the dp never free-wraps — the
                # single crossing per lap is placed authoritatively at the S/F reset. No apex snap.
                self._r_prev[num] = speed
                self._r_dp[num] = min(self._r_dp[num] + ddp, 99.9)
                dp = self._r_dp[num]
                x, y = self._dist_pct_to_xy(dp)
                entry = [round(x, 1), round(y, 1), round(dp, 3), 1]   # [3]=1 → estimated (for (b) commit)
                if self._miss[num] <= MISS_SAMPLES:
                    self._r_buf.setdefault(num, []).append((clock_time, entry))
                else:
                    for ts, e in self._r_buf.pop(num, []):
                        self._bus.emit("position", {num: e}, ts)
                    recon[num] = entry
                continue

            if self._r_dp[num] + ddp >= 100.0:
                self._wrapped[num] = True             # natural S/F wrap this lap → no synthetic needed
            dp = (self._r_dp[num] + ddp) % 100.0

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
            if self._miss[num] <= MISS_SAMPLES:
                self._r_buf.setdefault(num, []).append((clock_time, entry))   # tolerate: buffer
            else:
                for ts, e in self._r_buf.pop(num, []):
                    self._bus.emit("position", {num: e}, ts)                   # backfill the onset
                recon[num] = entry

        if recon:
            self._bus.emit("position", recon, clock_time)
