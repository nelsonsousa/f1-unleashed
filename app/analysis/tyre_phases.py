"""Per-stint tyre phase detection (4-phase SME model).

For each driver's stint in a session, classify each lap into one of:
  - Phase 1 (warm-up): first N laps depending on compound.
  - Phase 2 (peak): smoothed lap-time delta ≤ 0 — tyres at temperature,
    pace improves with fuel burn-off (~0.1 s/lap).
  - Phase 3 (management): 0 < smoothed delta ≤ +0.5 s/lap — tyres
    starting to degrade gently.
  - Phase 4 (cliff): smoothed delta > +0.5 s/lap — rapid degradation.

Phase classification is per-lap (NOT sequential) — each lap reflects
the current local trend. Phases can oscillate due to traffic, SC restart,
strategy changes. The CLIFF is confirmed only by a sustained run.

Cliff confirmation: a "sustained cliff" requires CLIFF_RUN_LENGTH or more
consecutive non-outlier laps classified as Phase 4. Single-lap or short
Phase-4 runs are treated as noise (= SC artefact, traffic, lift & coast).
Tyre lifetime = lap before sustained cliff begins, or full stint if none.

Outlier filter: laps with lap_time > stint_median × OUTLIER_RATIO are
flagged as outliers (= SC, VSC, traffic). Outliers are excluded from
delta computation and from cliff-run counting.

Smoothing: 3-lap moving average of deltas between consecutive non-outlier
non-warm-up laps.

Stints come from driverTyres; lap times from driverLapTimes; lap
classification (OUT/IN/PIT) from lapClassification — laps in those
classes are excluded from the phase analysis.

Reads from session.db; writes to data/analysis/{year}/{event}/{session}/
tyre_phases.json via app.processing.analysis_store.

Phase 4 is almost always absent from FP, always absent from Q/SQ, and
only occasionally observed in races.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from pathlib import Path
from typing import Optional

from app.processing import analysis_store

logger = logging.getLogger(__name__)


# Warm-up laps per compound (= laps to skip before phase analysis).
# Excludes the OUT lap (which is already filtered by lap_classification).
WARMUP_LAPS_BY_COMPOUND = {
    "SOFT": 0,
    "MEDIUM": 1,
    "HARD": 2,
    "INTERMEDIATE": 0,  # wet stints — phase model doesn't apply cleanly
    "WET": 0,
}

# Smoothed-delta thresholds (in milliseconds per lap).
PHASE_2_MAX_DELTA_MS = 0       # peak: getting faster or flat
PHASE_3_MAX_DELTA_MS = 500     # management: ≤ +0.5 s/lap
# Above PHASE_3_MAX_DELTA_MS → Phase 4 (cliff). 0.5 s/lap matches the
# SME definition ("each lap is 0.5s slower than the lap before").

SMOOTH_WINDOW = 3       # laps in moving average for delta smoothing
OUTLIER_RATIO = 1.05    # lap_time > stint_median × this → outlier (SC/VSC/traffic)
CLIFF_RUN_LENGTH = 3    # consecutive non-outlier Phase 4 laps to confirm cliff


def _parse_ms(s: Optional[str]) -> Optional[int]:
    if not s or not isinstance(s, str):
        return None
    try:
        if ":" in s:
            m_part, rest = s.split(":", 1)
            sec_part, frac = (rest.split(".", 1) if "." in rest else (rest, "0"))
            return int(m_part) * 60_000 + int(sec_part) * 1000 + int(frac.ljust(3, "0")[:3])
        sec_part, frac = (s.split(".", 1) if "." in s else (s, "0"))
        return int(sec_part) * 1000 + int(frac.ljust(3, "0")[:3])
    except (ValueError, AttributeError):
        return None


def _format_ms(ms: Optional[float]) -> Optional[str]:
    if ms is None:
        return None
    m = int(ms // 60_000)
    s = (ms - m * 60_000) / 1000.0
    return f"{m}:{s:06.3f}"


def _load_latest_per_topic(conn: sqlite3.Connection, prefix: str) -> dict[str, dict]:
    """For wildcard-style per-driver topics (driverLapTimes:1, …), return
    {num: parsed_latest_data}. Latest = highest offset_ms."""
    latest: dict[str, tuple[int, str]] = {}
    for off, topic, data in conn.execute(
        f"SELECT offset_ms, topic, data FROM messages "
        f"WHERE topic LIKE '{prefix}:%' ORDER BY offset_ms"
    ):
        num = topic.split(":", 1)[1]
        if num not in latest or off > latest[num][0]:
            latest[num] = (off, data)
    out: dict[str, dict] = {}
    for num, (_, data) in latest.items():
        try:
            out[num] = json.loads(data)
        except json.JSONDecodeError:
            pass
    return out


def _load_driver_list(conn: sqlite3.Connection) -> dict[str, dict]:
    drv: dict[str, dict] = {}
    for (data,) in conn.execute(
        "SELECT data FROM messages WHERE topic='driverList'"
    ):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            for num, info in d.items():
                if isinstance(info, dict):
                    drv[num] = {
                        "tla": info.get("tla") or num,
                        "team": (info.get("teamName") or "").strip(),
                    }
    return drv


def _detect_phases(
    lap_times: list[tuple[int, int]],  # [(lap_num, lap_ms), …] ordered by lap
    warmup_laps: int,
) -> list[dict]:
    """Classify each lap into Phase 1/2/3/4. Phases are SEQUENTIAL (can
    only progress, never regress). Outliers (SC/VSC/traffic) inherit the
    prior phase and are excluded from delta computation.

    Returns a list of {lap, lap_ms, delta_ms, smoothed_delta_ms, phase,
    outlier} aligned with lap_times. The first non-warm-up lap has
    delta=null (no previous non-outlier lap to diff against).
    """
    if not lap_times:
        return []

    # Outlier flag: lap_time > stint_median × OUTLIER_RATIO.
    times = [ms for _, ms in lap_times]
    stint_median = statistics.median(times)
    outlier_max = stint_median * OUTLIER_RATIO
    is_outlier = [ms > outlier_max for _, ms in lap_times]

    # Deltas between consecutive non-outlier laps (post-warmup).
    deltas: list[Optional[int]] = [None] * len(lap_times)
    prev_idx: Optional[int] = None
    for i in range(len(lap_times)):
        if i < warmup_laps or is_outlier[i]:
            continue
        if prev_idx is not None:
            deltas[i] = lap_times[i][1] - lap_times[prev_idx][1]
        prev_idx = i

    # Smoothed delta = moving average over last SMOOTH_WINDOW non-null deltas.
    smoothed: list[Optional[float]] = [None] * len(lap_times)
    buf: list[int] = []
    for i, d in enumerate(deltas):
        if d is None:
            continue
        buf.append(d)
        if len(buf) > SMOOTH_WINDOW:
            buf.pop(0)
        smoothed[i] = sum(buf) / len(buf)

    # Per-lap phase classification (NOT sequential). Each lap reflects
    # the current local trend.
    out = []
    last_phase: Optional[int] = None
    for i, (lap, lap_ms) in enumerate(lap_times):
        if i < warmup_laps:
            phase = 1
        elif is_outlier[i]:
            # Outliers inherit the previous phase classification (don't
            # contribute to phase classification).
            phase = last_phase if last_phase is not None else 2
        else:
            sd = smoothed[i]
            if sd is None:
                phase = 2  # first non-outlier post-warmup lap, no delta yet
            elif sd <= PHASE_2_MAX_DELTA_MS:
                phase = 2
            elif sd <= PHASE_3_MAX_DELTA_MS:
                phase = 3
            else:
                phase = 4
        if not is_outlier[i] and i >= warmup_laps:
            last_phase = phase
        out.append({
            "lap": lap,
            "lap_ms": lap_ms,
            "delta_ms": deltas[i],
            "smoothed_delta_ms": (int(smoothed[i]) if smoothed[i] is not None else None),
            "phase": phase,
            "outlier": is_outlier[i],
        })
    return out


def _stint_regression_slope_s_per_lap(classified: list[dict]) -> Optional[float]:
    """Linear regression slope of lap_time vs lap_in_stint, restricted to
    non-warmup non-outlier laps. Returns slope in seconds per lap.

    Much more robust than mean-of-deltas because per-lap variance from
    traffic / fuel saving / small mistakes averages out across the stint
    rather than being treated as degradation.
    """
    points = [(i, float(c["lap_ms"]))
              for i, c in enumerate(classified)
              if c["phase"] != 1 and not c["outlier"]]
    n = len(points)
    if n < 3:
        return None
    sum_x = sum(p[0] for p in points)
    sum_y = sum(p[1] for p in points)
    sum_xx = sum(p[0] * p[0] for p in points)
    sum_xy = sum(p[0] * p[1] for p in points)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return None
    slope_ms_per_lap = (n * sum_xy - sum_x * sum_y) / denom
    return round(slope_ms_per_lap / 1000.0, 4)


def _find_sustained_cliff(classified: list[dict]) -> Optional[int]:
    """Find the index of the FIRST lap in a sustained cliff run.

    A sustained cliff = CLIFF_RUN_LENGTH or more consecutive non-outlier
    laps classified as Phase 4. Returns None if no sustained cliff exists.
    """
    run_start: Optional[int] = None
    run_len = 0
    for i, c in enumerate(classified):
        if c["outlier"]:
            continue  # outliers don't break the run, but don't extend it either
        if c["phase"] == 4:
            if run_start is None:
                run_start = i
            run_len += 1
            if run_len >= CLIFF_RUN_LENGTH:
                return run_start
        else:
            run_start = None
            run_len = 0
    return None


def _stint_summary(
    classified: list[dict],
) -> dict:
    """Summarise per-stint phase boundaries + degradation slopes.

    Returns {phase_2: {start_lap, end_lap, n_laps, mean_lap_ms} | None,
             phase_3: {..., mean_degradation_s_per_lap} | None,
             phase_4: {start_lap, mean_degradation_s_per_lap} | None,
             lifetime_estimate_laps: int}
    """
    by_phase: dict[int, list[dict]] = {2: [], 3: [], 4: []}
    for c in classified:
        if c["phase"] in by_phase:
            by_phase[c["phase"]].append(c)

    def _summary_2() -> Optional[dict]:
        laps = by_phase[2]
        if not laps:
            return None
        non_outlier = [l for l in laps if not l["outlier"]]
        lap_mss = [l["lap_ms"] for l in non_outlier] or [l["lap_ms"] for l in laps]
        return {
            "start_lap": laps[0]["lap"],
            "end_lap": laps[-1]["lap"],
            "n_laps": len(laps),
            "mean_lap_ms": int(sum(lap_mss) / len(lap_mss)),
        }

    def _summary_3() -> Optional[dict]:
        laps = by_phase[3]
        if not laps:
            return None
        # Use non-outlier deltas to compute the degradation rate.
        deltas = [l["delta_ms"] for l in laps
                  if l["delta_ms"] is not None and not l["outlier"]]
        non_outlier = [l for l in laps if not l["outlier"]]
        end_lap_ms = (non_outlier[-1]["lap_ms"] if non_outlier
                      else laps[-1]["lap_ms"])
        return {
            "start_lap": laps[0]["lap"],
            "end_lap": laps[-1]["lap"],
            "n_laps": len(laps),
            "end_lap_ms": end_lap_ms,
            "mean_degradation_s_per_lap": (
                round(sum(deltas) / len(deltas) / 1000.0, 3) if deltas else None
            ),
        }

    def _summary_4() -> Optional[dict]:
        laps = by_phase[4]
        if not laps:
            return None
        deltas = [l["delta_ms"] for l in laps
                  if l["delta_ms"] is not None and not l["outlier"]]
        return {
            "start_lap": laps[0]["lap"],
            "end_lap": laps[-1]["lap"],
            "n_laps": len(laps),
            "mean_degradation_s_per_lap": (
                round(sum(deltas) / len(deltas) / 1000.0, 3) if deltas else None
            ),
        }

    # Lifetime estimate = laps before sustained cliff begins. If no
    # sustained cliff (= CLIFF_RUN_LENGTH+ consecutive Phase 4 laps),
    # lifetime = full stint length.
    cliff_idx = _find_sustained_cliff(classified)
    stint_start = classified[0]["lap"]
    if cliff_idx is not None:
        cliff_first_lap = classified[cliff_idx]["lap"]
        lifetime = max(0, cliff_first_lap - stint_start)
    else:
        lifetime = classified[-1]["lap"] - stint_start + 1

    return {
        "phase_2": _summary_2(),
        "phase_3": _summary_3(),
        "phase_4": _summary_4(),
        "lifetime_estimate_laps": lifetime,
        "cliff_detected": cliff_idx is not None,
        "cliff_first_lap": (classified[cliff_idx]["lap"]
                            if cliff_idx is not None else None),
    }


def analyze_session(session_path: Path) -> Optional[dict]:
    """Build the tyre_phases analysis for a single session.

    Reads session.db, runs phase detection per driver per stint, returns
    the structured analysis (does NOT write to disk — caller saves via
    analysis_store).
    """
    db = session_path / "session.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        drv = _load_driver_list(conn)
        lap_times_raw = _load_latest_per_topic(conn, "driverLapTimes")
        lap_cls_raw = _load_latest_per_topic(conn, "lapClassification")
        tyres_raw = _load_latest_per_topic(conn, "driverTyres")
    finally:
        conn.close()

    stints_out: list[dict] = []
    for num, info in sorted(drv.items()):
        tyres = tyres_raw.get(num)
        if not isinstance(tyres, list):
            continue
        lt_raw = lap_times_raw.get(num) or {}
        lt: dict[int, int] = {}
        for k, v in lt_raw.items():
            try:
                ln = int(k)
            except (ValueError, TypeError):
                continue
            ms = _parse_ms(v)
            if ms is not None and ms > 0:
                lt[ln] = ms
        if not lt:
            continue
        lc: dict[int, str] = {}
        lc_raw = lap_cls_raw.get(num) or {}
        laps_map = lc_raw.get("laps") if isinstance(lc_raw, dict) else None
        if isinstance(laps_map, dict):
            for k, v in laps_map.items():
                try:
                    lc[int(k)] = v
                except (ValueError, TypeError):
                    pass

        # Build stint ranges (= start lap + length).
        ts = sorted(
            (t for t in tyres if isinstance(t, dict)),
            key=lambda s: s.get("lap", 0),
        )
        for i, s in enumerate(ts):
            start = s.get("lap")
            if start is None:
                continue
            comp = (s.get("compound") or "").upper()
            if i + 1 < len(ts):
                length = (ts[i + 1].get("lap", start) or start) - start
            else:
                length = (s.get("totalLaps") or 0) - (s.get("startLaps") or 0)
            if length <= 0:
                continue

            # Collect timed laps in stint (exclude OUT/IN/PIT).
            # Lap classifications:
            #   FP: OUT / PUSH / COOL / IN / LONG
            #   R:  OUT / RACE / IN / PIT  (= every racing lap is "RACE")
            #   Q/SQ: usually OUT / PUSH / COOL / IN
            # We treat "LONG" and "RACE" both as long-run race-pace laps.
            stint_laps: list[tuple[int, int]] = []
            n_long = n_push = n_cool = 0
            for ln in range(start, start + length):
                cls = lc.get(ln, "")
                if cls in ("OUT", "IN", "PIT"):
                    continue
                ms = lt.get(ln)
                if ms is None:
                    continue
                stint_laps.append((ln, ms))
                if cls in ("LONG", "RACE"): n_long += 1
                elif cls == "PUSH": n_push += 1
                elif cls == "COOL": n_cool += 1
            if len(stint_laps) < 2:
                continue

            warmup = WARMUP_LAPS_BY_COMPOUND.get(comp, 1)
            classified = _detect_phases(stint_laps, warmup)
            summary = _stint_summary(classified)
            # Linear-regression slope over non-warmup non-outlier laps —
            # much more robust to per-lap variance than mean-of-deltas.
            regression_slope = _stint_regression_slope_s_per_lap(classified)

            stints_out.append({
                "driver_num": num,
                "driver_tla": info["tla"],
                "team": info["team"] or f"#{num}",
                "compound": comp or "UNKNOWN",
                "stint_start_lap": start,
                "stint_length": length,
                "n_timed_laps": len(stint_laps),
                "n_long": n_long,
                "n_push": n_push,
                "n_cool": n_cool,
                # Stress-equivalent laps: race-pace-equivalent tyre wear.
                # LONG = 1, PUSH ≈ 4, COOL ≈ 0 (per SME — quali laps wear
                # tyres ~4× faster than race laps).
                "stress_equivalent_laps": n_long + 4 * n_push,
                "warmup_laps": warmup,
                "regression_slope_s_per_lap": regression_slope,
                "laps": classified,
                **summary,
            })

    return {
        "session_name": session_path.name,
        "phase_definitions": {
            "phase_2_max_delta_ms": PHASE_2_MAX_DELTA_MS,
            "phase_3_max_delta_ms": PHASE_3_MAX_DELTA_MS,
            "smooth_window": SMOOTH_WINDOW,
            "warmup_laps_by_compound": WARMUP_LAPS_BY_COMPOUND,
        },
        "stints": stints_out,
    }


def analyze_and_save(session_path: Path) -> Optional[Path]:
    """Run analysis + persist to data/analysis/.../tyre_phases.json."""
    result = analyze_session(session_path)
    if not result:
        return None
    return analysis_store.save(session_path, "tyre_phases", result)
