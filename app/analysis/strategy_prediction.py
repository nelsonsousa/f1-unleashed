"""Race strategy prediction — computed at Qualifying finalize.

For the upcoming Race only (= not Sprint), enumerates 1-stop and 2-stop
strategies per team, picks the lowest expected total race time. Uses:

  * Per-team per-compound race pace (from this Q session's
    `pecking_order.json` → `race_pace_by_compound`).
  * Per-compound tyre degradation (slope) and lifetime — pooled from
    previous events' `tyre_phases.json` race+sprint stints.
  * Fixed pit loss of 25 s per stop.
  * Race lap count from a per-event constant table (= known circuit info,
    not future data).

Output schema saved as `strategy_prediction.json`:

  {
    "session_name": <Q session name>,
    "race_laps": int,
    "pit_loss_s": 25,
    "compound_assumptions": {
      "MEDIUM": {"lifetime_laps": 25, "degradation_s_per_lap": 0.09},
      ...
    },
    "predictions": [
      {
        "team": str, "color": str, "cohort": str,
        "optimal": <strategy>,
        "alternatives": [ <strategy>, … (top 4) ],
      }, …
    ]
  }

Each `strategy` row:
  {
    "stops": 1 | 2,
    "sequence": ["MEDIUM", "HARD"],     # compound order
    "stint_lengths": [25, 31],          # in laps
    "pit_laps": [25],                   # laps when pit stops happen
    "total_time_s": float,              # estimated total race time
    "gap_to_optimal_s": float,          # vs team's optimal
  }

F1 rule: dry race requires ≥ 2 distinct compounds. Strategies that don't
satisfy this are excluded.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.processing import analysis_store
from app.processing.processors.pace_processor import _pace_band_cohort

logger = logging.getLogger(__name__)


PIT_LOSS_S = 22.0  # default if no measurement available for this event

# Per-SME: practice stints under-utilise tyres (= teams shelve as soon
# as performance drops, not at the cliff). Race-equivalent lifetime is
# 2-3× the observed practice stint. Use the lower bound here.
PRACTICE_LIFE_MULTIPLIER = 2.5

# Sprint stints: closer to truth than practice, but teams may still pit
# strategically before cliff. Apply a modest multiplier.
SPRINT_LIFE_MULTIPLIER = 1.3

# Per-SME compound lifetime adjustments. Tyres last LONGER in a race
# than what we measure from earlier sessions (= teams shelve early in
# practice; sprint stints often end strategically before the cliff).
# Apply this AFTER any other lifetime computation.
LIFETIME_RACE_MULTIPLIER = {
    "SOFT": 1.0,
    "MEDIUM": 1.25,
    "HARD": 1.50,
    "INTERMEDIATE": 1.0,
    "WET": 1.0,
}

# Fuel burn-off subsidy: a race car gets ~0.04 s/lap faster purely from
# fuel weight loss (~1.7 kg/lap × ~0.025 s/kg). Without subtracting it,
# the regression slope mixes fuel benefit with tyre wear, making "deg"
# look negative on long stints. We add it back to isolate tyre-only deg.
FUEL_BENEFIT_S_PER_LAP = 0.04

# Minimum viable stint length (= avoid 1-lap "I just satisfied the
# 2-compound rule" degenerate optimizer solutions).
MIN_STINT_LAPS = 6

# Start-tyre bias (per SME): teams overwhelmingly prefer MEDIUM at race
# start. Hard suffers slow getaway (= lose track position); Soft suffers
# high-fuel deg. These penalties express the bias as added time on the
# total — close-call strategies tilt toward M-start.
START_PENALTY_S = {
    "MEDIUM": 0.0,        # baseline
    "HARD": 6.0,          # slow getaway, lost track position
    # Soft: rapid high-fuel deg, very rarely used at start (= Monaco-type
    # tracks only). Penalty matches the practical bias against it.
    "SOFT": 12.0,
    "INTERMEDIATE": 0.0,  # wet race, doesn't apply
    "WET": 0.0,
}

# Default tyre lifetimes as a FRACTION of race lap count. Per SME, FIA
# picks compounds with: Medium ≈ 40% of race; Hard ≈ 60-70%; Soft short.
# Used as a floor when this event's prior sessions don't yet have enough
# data to estimate lifetime empirically.
DEFAULT_LIFETIME_FRACTION = {
    # Tightened: high-fuel soft cliffs early; typical ~12 laps on
    # high-fuel even though FP one-shot looks faster.
    "SOFT": 0.18,
    "MEDIUM": 0.40,
    "HARD": 0.65,
    "INTERMEDIATE": 0.50,
    "WET": 0.50,
}

# Default per-compound degradation rates when this event has no data.
# Soft tightened: real-world high-fuel soft wears ~2× faster than M.
DEFAULT_DEG_S_PER_LAP = {
    "SOFT": 0.10,
    "MEDIUM": 0.04,
    "HARD": 0.03,
    "INTERMEDIATE": 0.05,
    "WET": 0.05,
}


# Race lap count per circuit (2026 schedule). Pre-known circuit info
# (length × target ~305 km), NOT race-day data. Update as new circuits
# are added to the season.
RACE_LAPS_BY_EVENT = {
    "1279_Melbourne": 58,
    "1280_Shanghai": 56,
    "1281_Suzuka": 53,
    "1284_Miami_Gardens": 57,
    "1285_Montréal": 70,
}




def _is_qualifying(session_path: Path) -> bool:
    name = session_path.name
    if "_" in name:
        head, _, rest = name.partition("_")
        if head.isdigit():
            name = rest
    return name == "Qualifying"


def _race_session_in_event(event_dir: Path) -> Optional[Path]:
    for s in event_dir.iterdir():
        if not s.is_dir():
            continue
        n = s.name
        if "_" in n:
            head, _, rest = n.partition("_")
            if head.isdigit():
                n = rest
        if n == "Race":
            return s
    return None


def _race_laps_for_event(event_name: str, event_dir: Path) -> int:
    """Return the race's lap count. First try the known table; fall back
    to reading the race session's SessionInfo if a Race folder exists."""
    if event_name in RACE_LAPS_BY_EVENT:
        return RACE_LAPS_BY_EVENT[event_name]
    # Soft-fallback: try to read NumberOfLaps from the race session.
    race_sess = _race_session_in_event(event_dir)
    if race_sess is None:
        return 60
    db = race_sess / "session.db"
    if not db.exists():
        return 60
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        row = conn.execute(
            "SELECT data FROM messages WHERE topic='SessionInfo' "
            "ORDER BY offset_ms LIMIT 1"
        ).fetchone()
        conn.close()
        if row:
            info = json.loads(row[0])
            n = info.get("NumberOfLaps") or info.get("ScheduledLaps")
            if isinstance(n, int) and n > 0:
                return n
    except Exception:
        logger.exception("Could not read race lap count for %s", event_name)
    return 60


def _circuit_name(event_folder: str) -> Optional[str]:
    """Extract circuit name from a folder like '1279_Melbourne' → 'Melbourne'."""
    if "_" not in event_folder:
        return None
    head, _, rest = event_folder.partition("_")
    return rest if head.isdigit() and rest else None


def _pit_loss_for_event(event_dir: Path) -> float:
    """Median pit-loss (s) for THIS circuit, measured from prior years'
    races at the same track. Pit loss is CIRCUIT-DEPENDENT (per SME) —
    different events at different tracks don't share pit-loss data.

    Looks across data/analysis/{any_year}/<event with same circuit name>/
    for `pit_loss_measurement.json` files. If no historical data for this
    circuit exists, falls back to PIT_LOSS_S (22 s) default.

    Note: at the moment we typically only have the current year's data,
    so this returns the default for most events. As multi-year history
    accumulates, the lookup will start hitting.
    """
    analysis_root = event_dir.parent.parent
    if not analysis_root.is_dir():
        return PIT_LOSS_S
    target_circuit = _circuit_name(event_dir.name)
    if not target_circuit:
        return PIT_LOSS_S
    medians: list[float] = []
    for year_dir in analysis_root.iterdir():
        if not year_dir.is_dir():
            continue
        for ev in year_dir.iterdir():
            if not ev.is_dir() or ev == event_dir:
                continue
            if _circuit_name(ev.name) != target_circuit:
                continue
            for sess in ev.iterdir():
                if not sess.is_dir():
                    continue
                m = analysis_store.load(sess, "pit_loss_measurement")
                if m and m.get("n_measurements", 0) > 0:
                    medians.append(m["median_pit_loss_s"])
    if not medians:
        return PIT_LOSS_S
    return round(sum(medians) / len(medians), 2)


def _event_tyre_stats(event_dir: Path, race_laps: int) -> dict:
    """Aggregate tyre lifetimes + degradation slopes from THIS EVENT's
    Sprint session only. PER SME: F1 has 6 compound types (C1-C6); FIA
    picks 3 per race and labels SOFT/MEDIUM/HARD. Tyre data must be
    event-scoped (= never pool across events) AND race-type only
    (= practice stints aren't race-representative).

    Sources within this event:
      - Sprint: stress-equivalent laps × SPRINT_LIFE_MULTIPLIER (1.3×).
      - Practice / Qualifying / Sprint-Qualifying: skipped — drivers
        don't run race-pace stints; data biases lifetime + degradation.

    Lifetime = MAX across all stints (longest proven survival).
    Degradation = mean regression slope from race-pace-rich stints only
                  (= n_long ≥ 5), with fuel-burn (+0.04 s/lap) added back
                  to isolate tyre-only deg, clamped at 0.

    If no Sprint data exists, lifetime falls back to
    DEFAULT_LIFETIME_FRACTION × race_laps; deg to DEFAULT_DEG_S_PER_LAP.

    Returns {COMPOUND: {"lifetime_laps", "degradation_s_per_lap",
                        "n_lifetime_samples", "n_deg_samples",
                        "source"}}.
    """
    per_compound_lifes: dict[str, list[float]] = defaultdict(list)
    per_compound_degs: dict[str, list[float]] = defaultdict(list)
    for sess in sorted(event_dir.iterdir()):
        if not sess.is_dir():
            continue
        n = sess.name
        if "_" in n:
            head, _, rest = n.partition("_")
            if head.isdigit():
                n = rest
        # Race-type sessions only. Sprint is the only one available
        # pre-Race (= we're predicting at Q finalize); the Race itself
        # is the FUTURE and we cannot peek at it.
        if n != "Sprint":
            continue
        multiplier = SPRINT_LIFE_MULTIPLIER
        tp = analysis_store.load(sess, "tyre_phases")
        if not tp:
            continue
        for s in tp.get("stints", []):
            comp = (s.get("compound") or "").upper()
            if not comp:
                continue
            stress = s.get("stress_equivalent_laps")
            if stress is None or stress == 0:
                stress = s.get("lifetime_estimate_laps", 0)
            if stress > 0:
                per_compound_lifes[comp].append(stress * multiplier)
            # Degradation from race-pace-rich stints only.
            if s.get("n_long", 0) >= 5:
                slope = s.get("regression_slope_s_per_lap")
                if slope is not None:
                    tyre_only = max(0.0, slope + FUEL_BENEFIT_S_PER_LAP)
                    per_compound_degs[comp].append(tyre_only)

    out: dict[str, dict] = {}
    all_compounds = (set(per_compound_lifes) | set(per_compound_degs) |
                     set(DEFAULT_LIFETIME_FRACTION))
    for comp in all_compounds:
        life_list = per_compound_lifes.get(comp) or []
        deg_list = per_compound_degs.get(comp) or []
        default_life = int(round(
            DEFAULT_LIFETIME_FRACTION.get(comp, 0.4) * race_laps
        ))
        default_deg = DEFAULT_DEG_S_PER_LAP.get(comp, 0.04)
        empirical_life = int(max(life_list)) if life_list else 0
        # Apply per-SME race-life multiplier (Medium 1.25×, Hard 1.5×).
        race_multiplier = LIFETIME_RACE_MULTIPLIER.get(comp, 1.0)
        adjusted_life = int(round(
            max(empirical_life, default_life) * race_multiplier
        ))
        out[comp] = {
            "lifetime_laps": adjusted_life,
            "degradation_s_per_lap": (
                round(sum(deg_list) / len(deg_list), 3) if deg_list
                else default_deg
            ),
            "n_lifetime_samples": len(life_list),
            "n_deg_samples": len(deg_list),
            "source": ("empirical" if (life_list or deg_list)
                       else "default_scaled_to_race_laps"),
        }
    return out


def _stint_time_s(
    laps: int,                       # stint length in laps
    base_lap_ms: int,                # team's pace on this compound (ms)
    deg_s_per_lap: float,            # phase-3 degradation rate
) -> float:
    """Sum of lap times over a stint, assuming linear degradation from
    a constant base pace. Lap k cost = base + (k - 1) × deg_ms.
    Total = laps × base + deg × laps × (laps - 1) / 2.
    """
    if laps <= 0:
        return 0.0
    base_s = base_lap_ms / 1000.0
    return laps * base_s + deg_s_per_lap * laps * (laps - 1) / 2.0


def _optimal_pit_lap(
    base_a_ms: int, deg_a: float, life_a: int,
    base_b_ms: int, deg_b: float, life_b: int,
    race_laps: int, pit_loss_s: float,
) -> Optional[tuple[int, float]]:
    """Optimal pit lap N for a 1-stop A→B given linear degradation.

    Total time T(N) = N × base_a + deg_a × N (N-1)/2
                    + (R-N) × base_b + deg_b × (R-N)(R-N-1)/2 + pit_loss
    Setting dT/dN = 0:
      base_a + deg_a × (N - 0.5) = base_b + deg_b × ((R - N) - 0.5)
      N = (base_b - base_a + 0.5 × (deg_b - deg_a) + R × deg_b) / (deg_a + deg_b)
    Search the integer N ∈ [1, R-1] that minimises T(N), respecting
    life_a (stint 1 ≤ life_a) and life_b (R - N ≤ life_b).
    Returns (best_N, best_total_s) or None if infeasible.
    """
    lo = max(MIN_STINT_LAPS, race_laps - life_b)
    hi = min(race_laps - MIN_STINT_LAPS, life_a)
    if lo > hi:
        return None
    best_n, best_t = None, float("inf")
    for n in range(lo, hi + 1):
        t = (
            _stint_time_s(n, base_a_ms, deg_a)
            + _stint_time_s(race_laps - n, base_b_ms, deg_b)
            + pit_loss_s
        )
        if t < best_t:
            best_n, best_t = n, t
    return (best_n, best_t) if best_n is not None else None


def _start_penalty(seq: list[str]) -> float:
    """Bias penalty for the chosen starting compound (per SME)."""
    if not seq:
        return 0.0
    return START_PENALTY_S.get(seq[0], 0.0)


def _optimal_two_stops(
    compounds: dict[str, dict],         # {COMP: {lap_ms, ...}}
    deg_by_comp: dict[str, float],
    life_by_comp: dict[str, int],
    seq: tuple[str, str, str],
    race_laps: int, pit_loss_s: float,
) -> Optional[dict]:
    """Optimal pit laps for a 2-stop (A, B, C) sequence by enumeration."""
    a, b, c = seq
    pace_a = compounds[a]["lap_ms"]
    pace_b = compounds[b]["lap_ms"]
    pace_c = compounds[c]["lap_ms"]
    la, lb, lc = life_by_comp[a], life_by_comp[b], life_by_comp[c]
    da, db, dc = deg_by_comp[a], deg_by_comp[b], deg_by_comp[c]

    best = None
    # Stint 1 length n1 must be ≥ MIN_STINT_LAPS and ≤ life_a.
    for n1 in range(MIN_STINT_LAPS, min(la, race_laps - 2 * MIN_STINT_LAPS) + 1):
        for n2 in range(n1 + MIN_STINT_LAPS, race_laps - MIN_STINT_LAPS + 1):
            stint_b = n2 - n1
            stint_c = race_laps - n2
            if stint_b > lb or stint_c > lc:
                continue
            if stint_b < MIN_STINT_LAPS or stint_c < MIN_STINT_LAPS:
                continue
            t = (
                _stint_time_s(n1, pace_a, da)
                + _stint_time_s(stint_b, pace_b, db)
                + _stint_time_s(stint_c, pace_c, dc)
                + 2 * pit_loss_s
            )
            if best is None or t < best["total_time_s"]:
                best = {
                    "stops": 2,
                    "sequence": [a, b, c],
                    "stint_lengths": [n1, stint_b, stint_c],
                    "pit_laps": [n1, n2],
                    "total_time_s": round(t, 1),
                }
    return best


def _enumerate_strategies_for_team(
    compounds_data: dict[str, dict],     # team's per-compound pace
    tyre_assumptions: dict[str, dict],
    race_laps: int, pit_loss_s: float,
) -> list[dict]:
    """All viable strategies for this team. Sorted by total_time_s."""
    available = list(compounds_data.keys())
    if len(available) == 0:
        return []
    deg_by_comp = {c: tyre_assumptions.get(c, {}).get("degradation_s_per_lap", 0.1)
                   for c in available}
    life_by_comp = {c: tyre_assumptions.get(c, {}).get("lifetime_laps", 20)
                    for c in available}

    strategies: list[dict] = []

    # 1-stop: A → B (A ≠ B).
    for a in available:
        for b in available:
            if a == b:
                continue
            res = _optimal_pit_lap(
                compounds_data[a]["lap_ms"], deg_by_comp[a], life_by_comp[a],
                compounds_data[b]["lap_ms"], deg_by_comp[b], life_by_comp[b],
                race_laps, pit_loss_s,
            )
            if res is None:
                continue
            n, total = res
            seq = [a, b]
            start_bias = _start_penalty(seq)
            strategies.append({
                "stops": 1,
                "sequence": seq,
                "stint_lengths": [n, race_laps - n],
                "pit_laps": [n],
                "total_time_s": round(total + start_bias, 1),
                "start_penalty_s": start_bias,
            })

    # 2-stop: enumerate (A, B, C) with ≥ 2 distinct compounds.
    for a in available:
        for b in available:
            for c in available:
                if len({a, b, c}) < 2:
                    continue
                res = _optimal_two_stops(
                    compounds_data, deg_by_comp, life_by_comp,
                    (a, b, c), race_laps, pit_loss_s,
                )
                if res is not None:
                    start_bias = _start_penalty(res["sequence"])
                    res["total_time_s"] = round(res["total_time_s"] + start_bias, 1)
                    res["start_penalty_s"] = start_bias
                    strategies.append(res)

    strategies.sort(key=lambda s: s["total_time_s"])
    return strategies


def compute(quali_session_path: Path) -> Optional[dict]:
    """Build strategy prediction for the upcoming Race. Returns None if
    this is not a Qualifying session or if data is insufficient."""
    if not _is_qualifying(quali_session_path):
        return None

    event_dir = quali_session_path.parent
    event_name = event_dir.name
    race_laps = _race_laps_for_event(event_name, event_dir)
    pit_loss = _pit_loss_for_event(event_dir)

    po = analysis_store.load(quali_session_path, "pecking_order")
    if not po:
        return None
    rpb = po.get("race_pace_by_compound") or []
    if not rpb:
        return None

    # Per SME: tyre compounds are PER-RACE (= FIA picks 3 of 6); never
    # pool tyre data across events. Use this event's FP + Sprint only.
    tyre_stats = _event_tyre_stats(event_dir, race_laps)

    predictions = []
    for entry in rpb:
        team = entry.get("team")
        comps = entry.get("compounds") or {}
        if not comps:
            continue
        # Coerce compound keys to uppercase for matching the tyre-assumption keys.
        comps_upper = {c.upper(): d for c, d in comps.items()}
        strats = _enumerate_strategies_for_team(comps_upper, tyre_stats, race_laps, pit_loss)
        if not strats:
            continue
        optimal = strats[0]
        alternatives = []
        for s in strats[1:]:
            s = dict(s)
            s["gap_to_optimal_s"] = round(s["total_time_s"] - optimal["total_time_s"], 1)
            alternatives.append(s)
            if len(alternatives) >= 4:
                break
        predictions.append({
            "team": team,
            "color": entry.get("color"),
            "cohort": entry.get("cohort"),
            "optimal": optimal,
            "alternatives": alternatives,
        })

    # Sort by optimal total time (= faster strategies first; matches the
    # race-pace pecking order in most cases, but may differ on tyre fit).
    predictions.sort(key=lambda p: p["optimal"]["total_time_s"])

    return {
        "session_name": quali_session_path.name,
        "race_laps": race_laps,
        "pit_loss_s": pit_loss,
        "compound_assumptions": tyre_stats,
        "predictions": predictions,
    }


def compute_and_save(quali_session_path: Path) -> Optional[Path]:
    result = compute(quali_session_path)
    if not result:
        return None
    return analysis_store.save(quali_session_path, "strategy_prediction", result)
