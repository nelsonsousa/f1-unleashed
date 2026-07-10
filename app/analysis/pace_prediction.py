"""v1 pace decomposition — a simple, transparent race-pace model.

Given a season's worth of :class:`~app.analysis.stint_dataset.LapRow` (race +
sprint laps), decompose lap time into interpretable pieces:

  * fuel burn        — a single s/lap benefit (compound-independent)
  * tyre degradation — a per-compound s/lap slope after the tyre's peak
  * compound offsets — how much faster SOFT is than MEDIUM, MEDIUM than HARD
  * race pace        — a per-team fuel+tyre-corrected clean-air lap time

Everything is deliberately kept staged and explicit — the intermediate numbers
(wear curves, pooled point counts, plateau slope) are all returned so the model
can be eyeballed and the constants below tuned by hand. This is v1: favour
simple + transparent over clever.

``compute_pace_model`` is a pure function (no I/O). The tunable constants are
module-level dicts so they are easy to edit.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Optional

from app.analysis.stint_dataset import LapRow

# ── tunable constants (all in units of SECONDS) ─────────────────────────────
# ages (0-based laps on the set) to drop as "not in the working window" — the
# tyre is still warming up and its lap times are not representative.
WARMUP = {"SOFT": 0, "MEDIUM": 1, "HARD": 3}
# age at which degradation is assumed to start (end of the plateau). Below this
# age the stint is on its "flat" part (fuel-dominated); at/above it, tyre wear
# dominates.
IMPROVE = {"SOFT": 6, "MEDIUM": 10, "HARD": 15}
# a lap counts as "clean air" only if the car ahead is more than this many
# seconds away (or the driver is the leader → interval None).
CLEAN_AIR_S = 3.0
# a stint needs at least this many representative laps to be usable.
MIN_STINT_LAPS = 8
# fuel benefit is clamped into [0, this] s/lap — a sanity bound.
FUEL_CAP_S_PER_LAP = 0.1

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")


# ── small numeric helpers ───────────────────────────────────────────────────

def _slope(points: list[tuple[float, float]]) -> Optional[float]:
    """Ordinary-least-squares slope of y vs x, or None if undetermined
    (<2 points or all x equal)."""
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    var = sum((x - mx) ** 2 for x, _ in points)
    if var == 0:
        return None
    cov = sum((x - mx) * (y - my) for x, y in points)
    return cov / var


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _median(vals: list[float]) -> Optional[float]:
    return statistics.median(vals) if vals else None


def _r3(v: Optional[float]) -> Optional[float]:
    return round(v, 3) if v is not None else None


def _r4(v: Optional[float]) -> Optional[float]:
    return round(v, 4) if v is not None else None


# ── the model ───────────────────────────────────────────────────────────────

def compute_pace_model(rows: list[LapRow]) -> dict:
    """Decompose ``rows`` into fuel / tyre-deg / compound-offset / race-pace.

    Pure: returns a dict of numbers, no I/O. See module docstring for the
    staged algorithm. Any quantity that can't be estimated (too few points)
    comes back as ``None`` rather than raising.
    """
    n_rows_total = len(rows)

    # ── stage 1: filter to representative clean-air green laps ──────────────
    kept: list[LapRow] = []
    drop_reasons: dict[str, int] = defaultdict(int)
    for r in rows:
        if r.lap_class != "":
            drop_reasons["lap_class"] += 1
            continue
        if r.compound not in COMPOUNDS:
            drop_reasons["compound"] += 1
            continue
        if not (r.interval_ahead_s is None or r.interval_ahead_s > CLEAN_AIR_S):
            drop_reasons["traffic"] += 1
            continue
        kept.append(r)

    # a common reference lap for fuel correction; cancels out of every
    # pairwise / gap comparison, so its exact value is immaterial — reported
    # for transparency.
    ref_lap = int(_median([r.lap_number for r in kept]) or 0)

    # ── stage 2: group into stints, keep the long ones ──────────────────────
    grouped: dict[tuple, list[LapRow]] = defaultdict(list)
    for r in kept:
        grouped[(r.event, r.session, r.driver, r.stint_idx)].append(r)
    good_stints: list[tuple[tuple, list[LapRow]]] = []
    for key, laps in grouped.items():
        if len(laps) >= MIN_STINT_LAPS:
            laps.sort(key=lambda r: r.tyre_age)
            good_stints.append((key, laps))

    # ── stage 3: pool (tyre_age, delta_s) per compound ──────────────────────
    # delta_s = lap_s - stint's own fastest representative lap.
    pooled: dict[str, list[tuple[int, float, int]]] = defaultdict(list)  # compound -> [(age, delta, lap_number)]
    stints_per_compound: dict[str, int] = defaultdict(int)
    for _key, laps in good_stints:
        compound = laps[0].compound
        stints_per_compound[compound] += 1
        fastest_s = min(l.lap_time_ms for l in laps) / 1000.0
        for l in laps:
            pooled[compound].append((l.tyre_age, l.lap_time_ms / 1000.0 - fastest_s, l.lap_number))

    # fuel slope: pooled plateau points across ALL compounds (fuel is
    # compound-independent). A negative slope in the plateau = getting faster
    # as the tank empties → fuel benefit.
    plateau: list[tuple[float, float]] = []
    for compound, pts in pooled.items():
        lo, hi = WARMUP[compound], IMPROVE[compound]
        for age, delta, _ln in pts:
            if lo <= age < hi:
                plateau.append((float(age), delta))
    fuel_slope = _slope(plateau)
    fuel_s_per_lap = _clamp(-fuel_slope, 0.0, FUEL_CAP_S_PER_LAP) if fuel_slope is not None else 0.0

    # per-compound net-deg slope (over ages past IMPROVE), wear curve, deg.
    compounds_out: dict[str, dict] = {}
    for compound in COMPOUNDS:
        pts = pooled.get(compound, [])
        deg_pts = [(float(age), delta) for age, delta, _ln in pts if age >= IMPROVE[compound]]
        net_slope = _slope(deg_pts)
        deg = None if net_slope is None else max(0.0, net_slope + fuel_s_per_lap)

        # wear curve: pooled median delta at each observed tyre_age.
        by_age: dict[int, list[float]] = defaultdict(list)
        for age, delta, _ln in pts:
            by_age[age].append(delta)
        wear_curve = [[age, _r3(_median(by_age[age]))] for age in sorted(by_age)]
        lifetime = max((age for age, _d, _ln in pts), default=None)

        compounds_out[compound] = {
            "deg_s_per_lap": _r4(deg),
            "net_deg_slope": _r4(net_slope),
            "lifetime_laps": lifetime,
            "n_stints": stints_per_compound.get(compound, 0),
            "n_points": len(pts),
            "n_deg_points": len(deg_pts),
            "wear_curve": wear_curve,
        }

    # ── stage 4: compound offsets ───────────────────────────────────────────
    # per (event, driver): best fuel-corrected stint-fastest lap per compound.
    by_ed: dict[tuple, dict[str, float]] = defaultdict(dict)
    for key, laps in good_stints:
        event, _session, driver, _stint = key
        compound = laps[0].compound
        fl = min(laps, key=lambda l: l.lap_time_ms)
        adj = fl.lap_time_ms / 1000.0 + fuel_s_per_lap * (ref_lap - fl.lap_number)
        d = by_ed[(event, driver)]
        if compound not in d or adj < d[compound]:
            d[compound] = adj
    sm_deltas = [d["SOFT"] - d["MEDIUM"] for d in by_ed.values() if "SOFT" in d and "MEDIUM" in d]
    mh_deltas = [d["MEDIUM"] - d["HARD"] for d in by_ed.values() if "MEDIUM" in d and "HARD" in d]
    compound_offsets = {
        "SOFT-MEDIUM": _r3(_median(sm_deltas)),
        "MEDIUM-HARD": _r3(_median(mh_deltas)),
        "counts": {"SOFT-MEDIUM": len(sm_deltas), "MEDIUM-HARD": len(mh_deltas)},
    }

    # ── stage 5: per-driver race pace, aggregated to team ───────────────────
    driver_corr: dict[str, list[float]] = defaultdict(list)
    driver_meta: dict[str, tuple[str, str]] = {}
    for r in kept:
        deg = compounds_out[r.compound]["deg_s_per_lap"] or 0.0
        corrected = (
            r.lap_time_ms / 1000.0
            + fuel_s_per_lap * (ref_lap - r.lap_number)
            - deg * r.tyre_age
        )
        driver_corr[r.driver].append(corrected)
        driver_meta[r.driver] = (r.tla, r.team)

    drivers_out = []
    for driver, vals in driver_corr.items():
        tla, team = driver_meta[driver]
        drivers_out.append({
            "driver": driver,
            "tla": tla,
            "team": team,
            "lap_s": _r3(_median(vals)),
            "n_laps": len(vals),
        })
    drivers_out.sort(key=lambda d: d["lap_s"] if d["lap_s"] is not None else float("inf"))

    # team = its fastest driver.
    team_best: dict[str, dict] = {}
    for d in drivers_out:
        if d["lap_s"] is None:
            continue
        cur = team_best.get(d["team"])
        if cur is None or d["lap_s"] < cur["lap_s"]:
            team_best[d["team"]] = {"team": d["team"], "tla": d["tla"], "lap_s": d["lap_s"]}
    race_pace = sorted(team_best.values(), key=lambda t: t["lap_s"])
    if race_pace:
        fastest = race_pace[0]["lap_s"]
        for t in race_pace:
            t["gap_s"] = _r3(t["lap_s"] - fastest)

    return {
        "fuel_s_per_lap": _r4(fuel_s_per_lap),
        "compounds": compounds_out,
        "compound_offsets": compound_offsets,
        "race_pace": race_pace,
        "drivers": drivers_out,
        "meta": {
            "n_rows_total": n_rows_total,
            "n_rows_kept": len(kept),
            "n_dropped": n_rows_total - len(kept),
            "drop_reasons": dict(drop_reasons),
            "n_stints": len(good_stints),
            "ref_lap": ref_lap,
            "plateau_slope": _r4(fuel_slope),
            "n_plateau_points": len(plateau),
            "constants": {
                "WARMUP": WARMUP,
                "IMPROVE": IMPROVE,
                "CLEAN_AIR_S": CLEAN_AIR_S,
                "MIN_STINT_LAPS": MIN_STINT_LAPS,
                "FUEL_CAP_S_PER_LAP": FUEL_CAP_S_PER_LAP,
            },
        },
    }
