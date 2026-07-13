"""Geometric PIT-LANE TRANSIT time, refined across an event's FP/Q sessions.

We publish the pit-lane transit — the time to pass through the whole pit lane at the speed
limit plus a fixed 5 s (1 s to slow to a stop + 1 s to accelerate back + 3 s stationary). We do
NOT predict a pit-stop LOSS from FP/Q: that (transit minus the on-track equivalent) is too weak
to be meaningful pre-race. The LOSS is measured in-race by the pit-stop-loss processor.

The pit lane has a speed limit (80 km/h; Monaco 60) held constant by the field. From telemetry
we read where speed drops to the limit on entry and climbs back over it on exit; the track-%
span between those points × the circuit length is the pit-lane length, and length ÷ limit is the
drive-through time:

    transit = (span% × circuit_length_m) / (limit_kmh / 3.6) + 2 + 3   [seconds]

Validated vs F1 `PitLaneTimeCollection.Duration` on the RACE (5/9 within ±2 s), but from FP/Q it
undershoots ~2–4 s (slow cool-down / warm-up laps) and is unreliable where the pit-lane dp is
frozen (Shanghai) or the entry/exit geometry is odd (Monaco, Silverstone) — hence a capped
confidence. Refines each session (FP1→FP2→FP3→Q, event-scoped). Offline ref: `scripts/pit_lane_speed.py`.

Output schema (pit_loss_estimate.json):
  {
    "metric": "pit_lane_transit",
    "circuit", "session_name", "prior_session", "sessions_used",
    "speed_limit_kmh", "pit_lane_span_pct", "pit_lane_length_m",
    "drive_through_s", "brake_accel_s": 2, "stationary_s": 3,
    "pit_lane_transit_s", "confidence", "low_confidence_geometry", "n_samples",
    "_spans": [...], "_limits": [...],     # accumulated, event-scoped, for the next session
  }
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from collections import Counter
from pathlib import Path
from typing import Optional

from app.processing import analysis_store
from app.processing.database import transient_db_path
from app.analysis.pecking_order import (_ordered_sessions_in_event,
                                        _session_canonical_name, find_prior_session)

logger = logging.getLogger(__name__)

STOP_KMH = 5.0
ZONE_TOL = 5.0            # controlled zone = speed <= limit + this
BRAKE_ACCEL_S = 2.0      # +1 s braking + 1 s accelerating
STATIONARY_S = 3.0      # nominal tyre-change stationary time
_FPQ = {"Practice_1", "Practice_2", "Practice_3", "Sprint_Qualifying", "Qualifying"}

# Circuit lengths in METRES (real lap distance). Hand-maintained — extend for new circuits;
# without an entry a circuit yields no geometric length and the estimate is skipped.
CIRCUIT_LENGTH_M = {
    "Melbourne": 5278, "Shanghai": 5451, "Suzuka": 5807, "Miami_Gardens": 5412,
    "Montréal": 4361, "Monte_Carlo": 3337, "Barcelona": 4657, "Spielberg": 4318,
    "Silverstone": 5891,
}


def _circuit_of(session_path: Path) -> str:
    """'1287_Barcelona' -> 'Barcelona'."""
    name = session_path.parent.name
    head, sep, rest = name.partition("_")
    return rest if (sep and head.isdigit()) else name


# ── DB read ─────────────────────────────────────────────────────────────────
def _read(session_path: Path):
    """(telemetry {num:[(off,speed,dp)]}, status {num:[(off,value)]}) from the session DB."""
    db = transient_db_path(session_path)
    if not db.exists():
        return {}, {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    tel, st = {}, {}
    try:
        for (t,) in conn.execute(
                "SELECT DISTINCT topic FROM messages WHERE topic LIKE 'liveTelemetry:%'"):
            num = t.split(":")[1]
            ser = []
            for off, data in conn.execute(
                    "SELECT offset_ms, data FROM messages WHERE topic=? ORDER BY offset_ms", (t,)):
                try:
                    d = json.loads(data)
                except json.JSONDecodeError:
                    continue
                dp = d.get("dp")
                if dp is None:
                    continue
                s = d.get("speed")
                ser.append((off, s if isinstance(s, (int, float)) else None, float(dp)))
            tel[num] = ser
        for (t,) in conn.execute(
                "SELECT DISTINCT topic FROM messages WHERE topic LIKE 'driverStatus:%'"):
            num = t.split(":")[1]
            seq = []
            for off, data in conn.execute(
                    "SELECT offset_ms, data FROM messages WHERE topic=? ORDER BY offset_ms", (t,)):
                try:
                    v = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not seq or seq[-1][1] != v:
                    seq.append((off, v))
            st[num] = seq
    finally:
        conn.close()
    return tel, st


# ── geometry ─────────────────────────────────────────────────────────────────
def _pit_windows(seq):
    """PIT→OUT→TRACK cycles → [(t_pit, t_track)]."""
    out, i = [], 0
    while i < len(seq):
        off, v = seq[i]
        if v != "PIT":
            i += 1
            continue
        t_track = None
        j = i + 1
        hit_pit = aborted = False
        while j < len(seq):
            o2, v2 = seq[j]
            if v2 == "PIT":
                hit_pit = True
                break
            if v2 in ("RET", "STOP", "ELIMINATED"):
                aborted = True
                break
            if v2 == "TRACK":
                t_track = o2
                break
            j += 1
        i = j if hit_pit else j + 1
        if not aborted and t_track is not None:
            out.append((off, t_track))
    return out


def _detect_limit(pool):
    binned = Counter(int(round(s / 2.0)) * 2 for s in pool if 30 <= s <= 110)
    return binned.most_common(1)[0][0] if binned else None


def _stationary(seg):
    best = rs = re = None
    for o, s, _ in seg:
        if s is not None and s <= STOP_KMH:
            if rs is None:
                rs = o
            re = o
        elif rs is not None:
            if best is None or re - rs > best[1] - best[0]:
                best = (rs, re)
            rs = None
    if rs is not None and (best is None or re - rs > best[1] - best[0]):
        best = (rs, re)
    return best if best and best[1] - best[0] >= 500 else None


def _zone_dp(seg, limit, stat):
    """dp at the two edges of the controlled zone (speed <= limit+ZONE_TOL, containing the
    standstill), bridging up to 2 out-of-band samples. (dp_entry, dp_exit) or (None, None)."""
    tol = limit + ZONE_TOL
    t_ss, t_se = stat
    n = len(seg)
    i_ss = next((i for i, x in enumerate(seg) if x[0] >= t_ss), None)
    i_se = next((i for i in range(n - 1, -1, -1) if seg[i][0] <= t_se), None)
    if i_ss is None or i_se is None:
        return None, None

    def inband(s):
        return s is not None and s <= tol

    start, miss, i = i_ss, 0, i_ss
    while i > 0:
        i -= 1
        if inband(seg[i][1]):
            start, miss = i, 0
        elif (miss := miss + 1) > 2:
            break
    end, miss, i = i_se, 0, i_se
    while i < n - 1:
        i += 1
        if inband(seg[i][1]):
            end, miss = i, 0
        elif (miss := miss + 1) > 2:
            break
    return seg[start][2], seg[end][2]


def _iqr_keep(xs):
    xs2 = sorted(x for x in xs if x is not None)
    if len(xs2) < 8:
        return [x for x in xs if x is not None]
    q1, _, q3 = statistics.quantiles(xs2, n=4)
    lo, hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
    return [x for x in xs if x is not None and lo <= x <= hi]


def _session_spans(tel, st):
    """(limit, [span% per usable pit visit]) for one session."""
    pool, segs = [], []
    for num, seq in st.items():
        telser = tel.get(num, [])
        for t_pit, t_track in _pit_windows(seq):
            lo, hi = t_pit - 8000, t_track + 8000
            seg = [(o, s, d) for o, s, d in telser if lo <= o <= hi]
            if sum(1 for _, s, _ in seg if s is not None) >= 5:
                pool.extend(s for _, s, _ in seg if s is not None)
                segs.append(seg)
    limit = _detect_limit(pool)
    if limit is None:
        return None, []
    spans = []
    for seg in segs:
        stat = _stationary(seg)
        if stat is None:
            continue
        dpi, dpx = _zone_dp(seg, limit, stat)
        if dpi is None or dpx is None:
            continue
        span = (dpx - dpi) % 100.0
        if 0.8 < span < 40:
            spans.append(span)
    return limit, spans


# ── public entry points ───────────────────────────────────────────────────
def compute(session_path: Path) -> Optional[dict]:
    circuit = _circuit_of(session_path)
    if circuit not in CIRCUIT_LENGTH_M:
        logger.info("pit_loss_estimate: no circuit length for %s — skipping", circuit)
        return None
    canon = _session_canonical_name(session_path.name)
    if canon not in _FPQ:                         # geometry is only measured from FP/Q runs
        return None

    tel, st = _read(session_path)
    if not st:
        return None
    limit, spans = _session_spans(tel, st)

    # ── event-scoped accumulation: carry the prior session's spans/limits ──
    prior = find_prior_session(session_path)
    prior_name = prior.name if prior else None
    acc_spans, acc_limits, sessions = [], [], []
    if prior is not None and _session_canonical_name(prior.name) in _FPQ:
        pj = analysis_store.load(prior, "pit_loss_estimate")
        if pj:
            acc_spans = list(pj.get("_spans", []))
            acc_limits = list(pj.get("_limits", []))
            sessions = list(pj.get("sessions_used", []))
    if spans:
        acc_spans.extend(spans)
    if limit is not None:
        acc_limits.append(limit)
    sessions.append(canon)

    if not acc_spans or not acc_limits:
        return None
    limit_final = Counter(acc_limits).most_common(1)[0][0]
    kept = _iqr_keep(acc_spans)
    span_pct = statistics.median(kept)
    length_m = span_pct / 100.0 * CIRCUIT_LENGTH_M[circuit]
    drive_through = length_m / (limit_final / 3.6)
    est = drive_through + BRAKE_ACCEL_S + STATIONARY_S

    n = len(kept)
    spread = (statistics.pstdev(kept) if len(kept) > 1 else 0.0)
    # low confidence where the geometry is unreliable: slow-limit circuits (track speed ≈ pit
    # limit inflates the zone — Monaco), an implausibly long span, or a wide span spread.
    weird = (limit_final <= 60) or (span_pct > 15) or (spread > 4.0)
    # The geometric estimate is a PREDICTION with a known FP/Q undershoot bias (~2–4 s) and
    # circuit-dependent failure modes, so it is capped well below 1.0 — the in-race measurement
    # is the high-confidence figure. Scales with sample count; halved for weird geometry.
    confidence = round(min(1.0, n / 40.0) * (0.3 if weird else 0.6), 2)

    return {
        "metric": "pit_lane_transit",
        "circuit": circuit,
        "session_name": session_path.name,
        "prior_session": prior_name,
        "sessions_used": sessions,
        "speed_limit_kmh": limit_final,
        "pit_lane_span_pct": round(span_pct, 2),
        "pit_lane_length_m": round(length_m),
        "drive_through_s": round(drive_through, 1),
        "brake_accel_s": BRAKE_ACCEL_S,
        "stationary_s": STATIONARY_S,
        "pit_lane_transit_s": round(est, 1),
        "confidence": confidence,
        "low_confidence_geometry": bool(weird),
        "n_samples": n,
        "_spans": acc_spans,
        "_limits": acc_limits,
    }


def compute_and_save(session_path: Path):
    result = compute(session_path)
    if result is None:
        return None
    return analysis_store.save(session_path, "pit_loss_estimate", result)
