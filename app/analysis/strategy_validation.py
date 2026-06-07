"""Validate the strategy-prediction model against actual race outcomes.

For each completed Race session:
  1. Determine each driver's actual strategy (= tyre compound sequence)
     from driverTyres.
  2. Classify drivers by strategy. Identify the 2-3 most common.
  3. For each common strategy, pick the BEST FINISHER (per SME =
     highest race finish position) within that group.
  4. Compute mean position-delta (finish - start) per strategy.
  5. Match each team against the model's `strategy_prediction.json`
     from the Q session: did the team's actual best driver use the
     predicted optimal strategy?

Output `strategy_validation.json` saved on the Race session.

Filters:
  * Stints with length < MIN_STINT_LAPS (= probably an early exit or
    abnormally short stint due to damage / SC pit-window). Drivers with
    such tail stints flagged as "off_strategy" and excluded from per-
    strategy grouping unless ALL of their stints are normal.
  * Drivers who DNF (= no finish position) are excluded from finisher
    rankings but kept in the actual-strategies tally.

The strategy match check:
  * Compares the tyre compound SEQUENCE (e.g., M-H vs M-H-M).
  * Stop count.
  * Does NOT compare exact pit laps — within a few laps is typical.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from app.processing import analysis_store

logger = logging.getLogger(__name__)


MIN_STINT_LAPS = 3   # discount very short tail stints (DNF / SC anomaly)

# Heuristics for off-strategy detection:
#   * Final stint shorter than this is likely a late opportunistic stop
#     (= soft tyre for fastest lap, SC trigger, etc.) rather than the
#     driver's planned strategy.
LATE_STOP_FINAL_STINT_MAX = 8
#   * A pit stop in the first N laps is usually a crash / damage / SC
#     opportunistic stop, not the planned strategy.
EARLY_STOP_MAX_LAP = 5
#   * Top-N finishers are most likely to have followed the ideal
#     strategy; off-strategy drivers tend to be further back.
TOP_FINISHERS_THRESHOLD = 10


def _is_race(session_path: Path) -> bool:
    name = session_path.name
    if "_" in name:
        head, _, rest = name.partition("_")
        if head.isdigit():
            name = rest
    return name == "Race"


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


def _load_latest_per_topic(conn: sqlite3.Connection, prefix: str) -> dict[str, dict]:
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


def _final_positions(conn: sqlite3.Connection) -> dict[str, int]:
    """Return {driver_num: finish_position} from the latest `standings`
    topic (= ordered list of driver numbers at end of race)."""
    row = conn.execute(
        "SELECT data FROM messages WHERE topic='standings' "
        "ORDER BY offset_ms DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {}
    try:
        order = json.loads(row[0])
    except json.JSONDecodeError:
        return {}
    if not isinstance(order, list):
        return {}
    return {num: pos for pos, num in enumerate(order, 1) if isinstance(num, str)}


def _driver_strategy(tyre_stints: list[dict]) -> Optional[dict]:
    """Build a normalised strategy dict from a driver's tyre stints.

    Returns {"sequence": [comp, …], "stops": int, "pit_laps": [int, …],
             "stint_lengths": [int, …]} or None if no usable data.
    Filters out trailing stints shorter than MIN_STINT_LAPS (= damage /
    safety-car-pit-window anomalies).
    """
    if not isinstance(tyre_stints, list) or not tyre_stints:
        return None
    ts = sorted(
        (s for s in tyre_stints if isinstance(s, dict)),
        key=lambda s: s.get("lap", 0),
    )
    if not ts:
        return None
    # Compute stint lengths.
    parsed = []
    for i, s in enumerate(ts):
        start = s.get("lap")
        comp = (s.get("compound") or "").upper()
        if start is None or not comp:
            continue
        if i + 1 < len(ts):
            length = (ts[i + 1].get("lap", start) or start) - start
        else:
            # Use totalLaps - startLaps for the final stint.
            length = (s.get("totalLaps") or 0) - (s.get("startLaps") or 0)
        if length <= 0:
            continue
        parsed.append({"start_lap": start, "compound": comp, "length": length})
    if not parsed:
        return None
    # Drop trailing too-short stints (likely anomalies).
    while len(parsed) > 1 and parsed[-1]["length"] < MIN_STINT_LAPS:
        parsed.pop()
    sequence = [p["compound"] for p in parsed]
    pit_laps = [p["start_lap"] for p in parsed[1:]]  # lap when each stop took place
    stint_lengths = [p["length"] for p in parsed]
    return {
        "sequence": sequence,
        "stops": len(parsed) - 1,
        "pit_laps": pit_laps,
        "stint_lengths": stint_lengths,
    }


def _qualifying_session_for_race(race_session_path: Path) -> Optional[Path]:
    event_dir = race_session_path.parent
    for s in event_dir.iterdir():
        if not s.is_dir():
            continue
        n = s.name
        if "_" in n:
            head, _, rest = n.partition("_")
            if head.isdigit():
                n = rest
        if n == "Qualifying":
            return s
    return None


def compute(race_session_path: Path) -> Optional[dict]:
    """Build strategy_validation for a completed Race session."""
    if not _is_race(race_session_path):
        return None
    db = race_session_path / "session.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        drv = _load_driver_list(conn)
        tyres = _load_latest_per_topic(conn, "driverTyres")
        positions = _final_positions(conn)
    finally:
        conn.close()

    # Build per-driver strategy + finish.
    drivers_info: list[dict] = []
    for num, info in drv.items():
        ty = tyres.get(num)
        strat = _driver_strategy(ty if isinstance(ty, list) else [])
        if strat is None:
            continue
        finish = positions.get(num)
        # Flag off-strategy drivers (= probably not following the intended
        # plan due to crash / damage / SC opportunity / late soft attack).
        off_strategy_reasons = []
        stint_lengths = strat["stint_lengths"]
        pit_laps = strat["pit_laps"]
        if stint_lengths and stint_lengths[-1] < LATE_STOP_FINAL_STINT_MAX:
            off_strategy_reasons.append("late_short_stint")
        if pit_laps and pit_laps[0] <= EARLY_STOP_MAX_LAP:
            off_strategy_reasons.append("very_early_stop")
        # Wet sequence (= INT/WET in the chain) — race conditions changed.
        if any(c in ("INTERMEDIATE", "WET") for c in strat["sequence"]):
            off_strategy_reasons.append("wet_compound")
        drivers_info.append({
            "num": num,
            "tla": info["tla"],
            "team": info["team"] or f"#{num}",
            "finish_pos": finish,
            "off_strategy_reasons": off_strategy_reasons,
            **strat,
        })

    # Group by tyre-compound sequence.
    by_seq: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for d in drivers_info:
        by_seq[tuple(d["sequence"])].append(d)

    actual_strategies = []
    # Sort by usage count (= most-common first).
    for seq, group in sorted(by_seq.items(), key=lambda kv: -len(kv[1])):
        finishers = [d for d in group if d["finish_pos"] is not None]
        finish_positions = [d["finish_pos"] for d in finishers]
        best = min(finishers, key=lambda d: d["finish_pos"]) if finishers else None
        # Strategy is "wet" if it includes any INTERMEDIATE or WET compound.
        is_wet = any(c in ("INTERMEDIATE", "WET") for c in seq)
        actual_strategies.append({
            "sequence": list(seq),
            "stops": len(seq) - 1 if seq else 0,
            "n_drivers": len(group),
            "n_finishers": len(finishers),
            "mean_finish_pos": (
                round(sum(finish_positions) / len(finish_positions), 1)
                if finish_positions else None
            ),
            "median_finish_pos": (
                sorted(finish_positions)[len(finish_positions) // 2]
                if finish_positions else None
            ),
            "wet": is_wet,
            "drivers": [
                {
                    "tla": d["tla"], "team": d["team"],
                    "finish_pos": d["finish_pos"],
                    "pit_laps": d["pit_laps"],
                    "stint_lengths": d["stint_lengths"],
                }
                for d in sorted(group, key=lambda d: (d["finish_pos"] is None,
                                                      d["finish_pos"] or 99))
            ],
            "best_finisher": (
                {"tla": best["tla"], "team": best["team"],
                 "finish_pos": best["finish_pos"]} if best else None
            ),
        })

    # Compare against the Q session's strategy_prediction.json.
    q_session = _qualifying_session_for_race(race_session_path)
    sp = analysis_store.load(q_session, "strategy_prediction") if q_session else None
    predicted_vs_actual = []
    if sp:
        for pred in sp.get("predictions", []):
            team = pred["team"]
            opt = pred["optimal"]
            # Find this team's actual drivers and which strategy each used.
            team_drivers = [d for d in drivers_info if d["team"] == team]
            if not team_drivers:
                continue
            finishers = [d for d in team_drivers if d["finish_pos"] is not None]
            best_driver = (min(finishers, key=lambda d: d["finish_pos"])
                           if finishers else None)
            best_strat = best_driver["sequence"] if best_driver else None
            pred_seq = opt["sequence"]
            exact_match = (best_strat == pred_seq) if best_strat else False
            # Compounds match = same set of compounds used (regardless
            # of order OR stop count). Captures "right tyre choice, just
            # different number of pit stops or order".
            compounds_match = (
                set(best_strat) == set(pred_seq) if best_strat else False
            )
            stops_match = (best_driver["stops"] == opt["stops"]) if best_strat else False
            predicted_vs_actual.append({
                "team": team,
                "predicted_optimal": {
                    "stops": opt["stops"],
                    "sequence": opt["sequence"],
                    "pit_laps": opt["pit_laps"],
                },
                "actual_best_driver": (
                    {
                        "tla": best_driver["tla"],
                        "sequence": best_driver["sequence"],
                        "stops": best_driver["stops"],
                        "pit_laps": best_driver["pit_laps"],
                        "finish_pos": best_driver["finish_pos"],
                    } if best_driver else None
                ),
                "strategy_match": exact_match,
                "compounds_match": compounds_match,
                "stops_match": stops_match,
            })

    pred_counter: Counter = Counter()
    for p in predicted_vs_actual:
        pred_counter[tuple(p["predicted_optimal"]["sequence"])] += 1
    dominant_predicted = (
        list(pred_counter.most_common(1)[0][0]) if pred_counter else None
    )

    def _dominant_actual(driver_subset: list[dict]) -> Optional[list[str]]:
        """Most-common dry strategy among the given driver subset."""
        c: Counter = Counter()
        for d in driver_subset:
            seq = tuple(d["sequence"])
            if not seq or any(x in ("INTERMEDIATE", "WET") for x in seq):
                continue
            c[seq] += 1
        if not c: return None
        top, n = c.most_common(1)[0]
        return list(top) if n >= 2 else None

    def _filter_stats(driver_subset: list[dict], label: str) -> dict:
        """Compute match counts (dominant + per-team) for this subset.

        Per-team metrics consider each team's BEST driver within the
        subset (= if both team drivers are off-strategy, the team is
        dropped; if only one is, we use that one).
        """
        # Per-team: pick the best in-subset driver, compare to prediction.
        per_team = defaultdict(list)
        for d in driver_subset:
            per_team[d["team"]].append(d)
        exact = compounds = stops = total = 0
        for team, group in per_team.items():
            with_finish = [d for d in group if d["finish_pos"] is not None]
            if not with_finish:
                continue
            best = min(with_finish, key=lambda d: d["finish_pos"])
            pred = next(
                (p for p in predicted_vs_actual if p["team"] == team), None
            )
            if not pred:
                continue
            total += 1
            pseq = pred["predicted_optimal"]["sequence"]
            if best["sequence"] == pseq:
                exact += 1
            if set(best["sequence"]) == set(pseq):
                compounds += 1
            if best["stops"] == pred["predicted_optimal"]["stops"]:
                stops += 1
        dom_actual = _dominant_actual(driver_subset)
        dom_match_exact = (dom_actual == dominant_predicted
                          if dom_actual and dominant_predicted else None)
        dom_match_comp = (set(dom_actual) == set(dominant_predicted)
                          if dom_actual and dominant_predicted else None)
        return {
            "label": label,
            "n_drivers": len(driver_subset),
            "n_teams": len(per_team),
            "dominant_actual_strategy": dom_actual,
            "dominant_match_exact": dom_match_exact,
            "dominant_match_compounds": dom_match_comp,
            "per_team_exact_matches": exact,
            "per_team_compound_matches": compounds,
            "per_team_stops_matches": stops,
            "per_team_total": total,
        }

    # All drivers (= baseline).
    all_drivers = drivers_info
    # On-strategy drivers (= no late short stint, no very early stop, no wet).
    on_strategy = [d for d in drivers_info if not d["off_strategy_reasons"]]
    # Top-10 finishers (= regardless of off-strategy flags).
    top10 = [d for d in drivers_info
             if d["finish_pos"] is not None
             and d["finish_pos"] <= TOP_FINISHERS_THRESHOLD]
    # Top-10 AND on-strategy (= strictest filter).
    top10_on_strategy = [d for d in top10 if not d["off_strategy_reasons"]]

    filters = [
        _filter_stats(all_drivers, "all"),
        _filter_stats(on_strategy, "on_strategy"),
        _filter_stats(top10, f"top{TOP_FINISHERS_THRESHOLD}"),
        _filter_stats(top10_on_strategy, f"top{TOP_FINISHERS_THRESHOLD}_on_strategy"),
    ]

    summary = {
        "n_drivers_classified": len(drivers_info),
        "n_distinct_strategies": len(by_seq),
        "exact_matches": sum(1 for p in predicted_vs_actual if p["strategy_match"]),
        "compounds_matches": sum(1 for p in predicted_vs_actual if p["compounds_match"]),
        "stops_matches": sum(1 for p in predicted_vs_actual if p["stops_match"]),
        "predicted_vs_actual_total": len(predicted_vs_actual),
        "dominant_predicted_strategy": dominant_predicted,
        "filters": filters,
    }

    return {
        "session_name": race_session_path.name,
        "summary": summary,
        "actual_strategies": actual_strategies,
        "predicted_vs_actual": predicted_vs_actual,
    }


def compute_and_save(race_session_path: Path) -> Optional[Path]:
    r = compute(race_session_path)
    if not r:
        return None
    return analysis_store.save(race_session_path, "strategy_validation", r)
