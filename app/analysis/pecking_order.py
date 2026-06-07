"""Pecking-order prediction per session.

At each session's finalize, builds a team-level pecking-order prediction
by blending:

  * The PRIOR session's `pecking_order.json` (= the running prediction
    accumulated through earlier sessions of the chain), and
  * THIS session's `pace.json` (= just-computed raw per-team pace).

The "chain" runs incrementally through an event and across events:

  * Non-sprint event: FP1 → FP2 → FP3 → Q → R.
  * Sprint event:     FP1 → SQ  → S  → Q → R.
  * First session of a new event seeds from the previous event's last
    session's `pecking_order.json`.

Blending uses per-session weights (FP1=1, FP2=2, FP3=4, SQ=4, S=8, Q=8,
R=8). Higher weight = the session's data dominates the running estimate.

Missing-data rule (per SME): if THIS session has no quali_pace or
race_pace data for a team, that metric's running prediction is
preserved unchanged. The prior gap and prior weight stay as-is.

Cohort assignment (per SME):

  * Cohort labels are imported from the previous EVENT's aggregated
    pace (= `load_previous_event_cohorts`).
  * Free Practice sessions NEVER change cohort. FP refines the gap
    within a cohort only.
  * Competitive sessions (Q/SQ/S/R) may promote/demote a team when
    BOTH the previous event's cohort AND this session's gap to P1
    point to the same NEW cohort. A single off-cohort observation
    doesn't move a team (= no sandbagging false positives).

Output schema (saved as pecking_order.json):

  {
    "session_type": str,
    "session_name": str,
    "prior_session": <str or null>,        # name of the session we built on
    "cohort_source_event": <str or null>,  # event whose pace defined cohorts
    "quali_pecking_order": [
      {"rank", "team", "color", "gap_s", "cohort",
       "confidence", "weight"}, …
    ],
    "race_pecking_order": [ … same shape … ],
    "race_pace_by_compound": [
      {"team", "color", "cohort", "compounds": {COMP: {gap_s, lap_ms,
       n_laps}}}, …      # race/sprint sessions only; empty otherwise
    ],
  }
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.processing import analysis_store
from app.processing.processors.pace_processor import (
    COHORT_LEAD_MAX_GAP_S,
    COHORT_BACK_MIN_GAP_S,
    _pace_band_cohort,
    load_previous_event_cohorts,
)

logger = logging.getLogger(__name__)


# Per-session blending weight. Higher = current data dominates the prior.
_SESSION_WEIGHT_BY_TYPE = {
    "Practice_1": 1,
    "Practice_2": 2,
    "Practice_3": 4,
    "Sprint_Qualifying": 4,
    "Sprint": 8,
    "Qualifying": 8,
    "Race": 8,
}

# Folder-name-based ordering within an event.
_NON_SPRINT_ORDER = ["Practice_1", "Practice_2", "Practice_3", "Qualifying", "Race"]
_SPRINT_ORDER = ["Practice_1", "Sprint_Qualifying", "Sprint", "Qualifying", "Race"]

_FP_SESSIONS = {"Practice_1", "Practice_2", "Practice_3"}
_COMPETITIVE_SESSIONS = {"Sprint_Qualifying", "Sprint", "Qualifying", "Race"}


def _session_canonical_name(folder_name: str) -> Optional[str]:
    """Strip leading session-id digits: '11234_Race' → 'Race'."""
    if "_" in folder_name:
        head, _, rest = folder_name.partition("_")
        if head.isdigit():
            return rest
    return folder_name


def _is_sprint_event(event_dir: Path) -> bool:
    for s in event_dir.iterdir():
        if not s.is_dir():
            continue
        if _session_canonical_name(s.name) == "Sprint_Qualifying":
            return True
    return False


def _ordered_sessions_in_event(event_dir: Path) -> list[Path]:
    """Return event's session folders in canonical chain order."""
    order = _SPRINT_ORDER if _is_sprint_event(event_dir) else _NON_SPRINT_ORDER
    by_canon: dict[str, Path] = {}
    for s in event_dir.iterdir():
        if not s.is_dir():
            continue
        canon = _session_canonical_name(s.name)
        if canon in order:
            by_canon[canon] = s
    return [by_canon[c] for c in order if c in by_canon]


def find_prior_session(session_path: Path) -> Optional[Path]:
    """Return the session immediately before ``session_path`` in the chain.

    Walks the current event's session order first; if ``session_path`` is
    the first session of its event, walks back to the previous event's
    last session. Returns None if no prior session exists in the cache.
    """
    event_dir = session_path.parent
    year_dir = event_dir.parent
    chain = _ordered_sessions_in_event(event_dir)
    try:
        idx = chain.index(session_path)
    except ValueError:
        return None
    if idx > 0:
        return chain[idx - 1]
    # FP1 of this event — look at previous event's last session.
    events = sorted((p for p in year_dir.iterdir() if p.is_dir()),
                    key=lambda p: p.name)
    try:
        eidx = events.index(event_dir)
    except ValueError:
        return None
    if eidx == 0:
        return None
    prev_event = events[eidx - 1]
    prev_chain = _ordered_sessions_in_event(prev_event)
    return prev_chain[-1] if prev_chain else None


def _entries_by_team(entries: list[dict]) -> dict[str, dict]:
    return {e["team"]: e for e in entries if e.get("team")}


def _blend_gap(
    prior_gap: Optional[float], prior_weight: float,
    current_gap: Optional[float], current_weight: float,
) -> tuple[Optional[float], float]:
    """Weighted average blend. Preserves prior on missing current data."""
    if current_gap is None:
        return prior_gap, prior_weight
    if prior_gap is None or prior_weight <= 0:
        return current_gap, current_weight
    blended = (prior_gap * prior_weight + current_gap * current_weight) / (
        prior_weight + current_weight
    )
    return blended, prior_weight + current_weight


def _apply_cohort_smoothing(
    cohort_prev_event: Optional[str],
    blended_gap: Optional[float],
    is_competitive: bool,
    prior_cohort: Optional[str],
) -> Optional[str]:
    """Apply SME cohort smoothing.

    * FP sessions never change cohort — inherit from the previous event
      (or the prior session's pecking_order if available).
    * Competitive sessions can move a team if BOTH:
        - The previous event's cohort agrees with the new band, OR
        - The prior session's running cohort agrees with the new band.
      (= single off-cohort observation doesn't move a team.)
    """
    if blended_gap is None:
        return prior_cohort or cohort_prev_event
    new_band = _pace_band_cohort(blended_gap)
    if not is_competitive:
        return prior_cohort or cohort_prev_event
    # Competitive: allow the move only if prior cohort or prev-event cohort
    # already pointed to the new band.
    if cohort_prev_event == new_band or prior_cohort == new_band:
        return new_band
    return prior_cohort or cohort_prev_event


def _build_metric_order(
    prior_entries: list[dict],          # prior pecking-order entries
    current_entries: list[dict],        # current session pace entries
    session_weight: float,
    cohorts_prev_event: dict[str, str],
    is_competitive: bool,
    color_lookup: dict[str, str],
    prior_session_same_event: bool = True,
) -> list[dict]:
    """Blend prior and current per-team gaps; assign cohorts; rank by gap.

    Per SME 2026-06-05: pace from the current event is *vastly* more
    important than observations from previous events because different
    circuits favour different teams. When the prior session is from a
    DIFFERENT event, ignore its pace gap entirely for ranking purposes —
    only the cohort (= leaders / midfield / backmarkers) carries across.
    Within the same event, the prior session's pace is still blended in.
    """
    prior_by_team = _entries_by_team(prior_entries)
    current_by_team = _entries_by_team(current_entries)
    all_teams = set(prior_by_team) | set(current_by_team)

    blended: dict[str, dict] = {}
    for team in all_teams:
        prior = prior_by_team.get(team) or {}
        cur = current_by_team.get(team) or {}
        # Cross-event: discard prior gap for ranking (= cohort is still
        # preserved via prior.get("cohort") + cohorts_prev_event below).
        if prior_session_same_event:
            prior_gap = prior.get("gap_s")
            prior_weight = float(prior.get("weight", 0) or 0)
        else:
            prior_gap = None
            prior_weight = 0.0
        cur_gap = cur.get("gap_s")
        cur_weight = session_weight if cur_gap is not None else 0
        new_gap, new_weight = _blend_gap(
            prior_gap, prior_weight, cur_gap, cur_weight,
        )
        if new_gap is None:
            continue
        new_cohort = _apply_cohort_smoothing(
            cohort_prev_event=cohorts_prev_event.get(team),
            blended_gap=new_gap,
            is_competitive=is_competitive,
            prior_cohort=prior.get("cohort"),
        )
        confidence = float(cur.get("confidence", 0) or prior.get("confidence", 0))
        blended[team] = {
            "team": team,
            "color": (cur.get("color") or prior.get("color")
                      or color_lookup.get(team) or "#888"),
            "gap_s": new_gap,
            "cohort": new_cohort,
            "confidence": round(confidence, 2),
            "weight": new_weight,
        }

    # Pure gap-based ordering (= SME 2026-06-07: drop cohort-clamping).
    # Cohorts are still recorded on each entry for any downstream
    # consumer that wants to know the historical label, but the rank /
    # display order is purely by gap_s. Rationale: cohort-clamping kept
    # incumbent leaders (= Mercedes / Red Bull) at the top of the rank
    # even when challengers (= McLaren, Ferrari) were demonstrably
    # closer to the leader. The displayed order should reflect actual
    # pace, not historical cohort inertia.
    ranked = sorted(blended.values(), key=lambda e: e["gap_s"])
    if not ranked:
        return []
    fastest_gap = ranked[0]["gap_s"]
    out = []
    for i, e in enumerate(ranked, 1):
        out.append({
            "rank": i,
            "team": e["team"],
            "color": e["color"],
            # Re-base to predicted P1 = 0.0 (= P1 by definition has zero gap).
            "gap_s": round(e["gap_s"] - fastest_gap, 3),
            "cohort": e["cohort"],
            "confidence": e["confidence"],
            "weight": e["weight"],
        })
    return out


def _synthesize_missing_compounds(merged: dict[str, dict]) -> dict[str, dict]:
    """Fill in missing compound entries per team using SME's rule:
    'MEDIUM sits half-way between SOFT and HARD' (= pace differential is
    roughly the same for every team).

    For each team that has SOFT + HARD but no MEDIUM: MEDIUM = mean(S, H).
    For teams missing SOFT or HARD with MEDIUM present, infer using the
    cross-team mean delta between those two compounds.

    Synthesized entries get source='synthesized' so consumers can
    distinguish them from empirical data.
    """
    # Cross-team per-compound mean lap_ms (= for inferring missing data).
    per_compound_means: dict[str, list[int]] = {}
    for team, comps in merged.items():
        for c, d in comps.items():
            ms = d.get("lap_ms")
            if ms is not None:
                per_compound_means.setdefault(c, []).append(ms)
    global_mean = {
        c: sum(vs) // len(vs) if vs else None
        for c, vs in per_compound_means.items()
    }

    for team, comps in merged.items():
        has = set(comps.keys())
        # SME midpoint rule: MEDIUM ≈ mean(SOFT, HARD).
        if "SOFT" in has and "HARD" in has and "MEDIUM" not in has:
            comps["MEDIUM"] = {
                "lap_ms": (comps["SOFT"]["lap_ms"] + comps["HARD"]["lap_ms"]) // 2,
                "n_laps": 0,
                "source": "synthesized_midpoint",
            }
            has.add("MEDIUM")
        # Fill HARD using HARD-MEDIUM cross-team delta if MEDIUM exists.
        if "MEDIUM" in has and "HARD" not in has and "HARD" in global_mean and "MEDIUM" in global_mean:
            delta = global_mean["HARD"] - global_mean["MEDIUM"]
            comps["HARD"] = {
                "lap_ms": comps["MEDIUM"]["lap_ms"] + delta,
                "n_laps": 0,
                "source": "synthesized_cross_team_delta",
            }
            has.add("HARD")
        # Fill SOFT using MEDIUM-SOFT cross-team delta if MEDIUM exists.
        if "MEDIUM" in has and "SOFT" not in has and "SOFT" in global_mean and "MEDIUM" in global_mean:
            delta = global_mean["SOFT"] - global_mean["MEDIUM"]
            comps["SOFT"] = {
                "lap_ms": comps["MEDIUM"]["lap_ms"] + delta,
                "n_laps": 0,
                "source": "synthesized_cross_team_delta",
            }
    return merged


def _build_compound_breakdown(
    prior_compound: list[dict],
    current_compound: list[dict],
    color_lookup: dict[str, str],
    final_cohorts: dict[str, str],
) -> list[dict]:
    """For race/sprint sessions, surface per-compound gaps to P1.

    Output one row per team with each compound's gap (in seconds) to the
    fastest team's pace on that same compound. Falls back to prior session
    data per (team, compound) when current session has none. Then
    synthesises missing compounds per the SME midpoint rule (= MEDIUM
    ≈ mean(SOFT, HARD)).
    """
    prior_by_team = {e["team"]: e for e in prior_compound}
    cur_by_team = {e["team"]: e for e in current_compound}
    all_teams = set(prior_by_team) | set(cur_by_team)
    # Build merged {team: {compound: {lap_ms, n_laps}}}
    merged: dict[str, dict] = {}
    for team in all_teams:
        prior_comps = (prior_by_team.get(team) or {}).get("compounds") or {}
        cur_comps = (cur_by_team.get(team) or {}).get("compounds") or {}
        comps: dict[str, dict] = {}
        for c in set(prior_comps) | set(cur_comps):
            cur_data = cur_comps.get(c)
            prior_data = prior_comps.get(c)
            chosen = cur_data if cur_data else prior_data
            if chosen:
                comps[c] = {
                    "lap_ms": chosen.get("lap_ms"),
                    "n_laps": chosen.get("n_laps", 0),
                    "source": "current" if cur_data else "prior",
                }
        if comps:
            merged[team] = comps

    # Fill missing compounds per SME's midpoint rule (= MEDIUM ≈ mean of
    # SOFT and HARD; SOFT or HARD inferred via cross-team delta if MEDIUM
    # is present). This lets strategy enumeration consider M-H strategies
    # even when a team didn't run MEDIUM in their FP long runs.
    merged = _synthesize_missing_compounds(merged)

    # Compute per-compound fastest across teams.
    fastest_by_comp: dict[str, int] = {}
    for team, comps in merged.items():
        for c, d in comps.items():
            ms = d.get("lap_ms")
            if ms is None:
                continue
            if c not in fastest_by_comp or ms < fastest_by_comp[c]:
                fastest_by_comp[c] = ms

    out = []
    for team, comps in merged.items():
        comp_out = {}
        for c, d in comps.items():
            ms = d.get("lap_ms")
            if ms is None or c not in fastest_by_comp:
                continue
            comp_out[c] = {
                "gap_s": round((ms - fastest_by_comp[c]) / 1000.0, 3),
                "lap_ms": ms,
                "n_laps": d["n_laps"],
                "source": d["source"],
            }
        if comp_out:
            out.append({
                "team": team,
                "color": (cur_by_team.get(team, {}).get("color") or
                          prior_by_team.get(team, {}).get("color") or
                          color_lookup.get(team) or "#888"),
                "cohort": final_cohorts.get(team),
                "compounds": comp_out,
            })
    # Sort by team's fastest compound gap to P1.
    out.sort(key=lambda e: min(c["gap_s"] for c in e["compounds"].values()))
    return out


def compute(session_path: Path) -> Optional[dict]:
    """Build pecking_order for a session (does not save).

    Returns None if there is no pace data for the session.
    """
    canon = _session_canonical_name(session_path.name)
    if canon is None:
        return None
    session_weight = _SESSION_WEIGHT_BY_TYPE.get(canon, 1)
    is_competitive = canon in _COMPETITIVE_SESSIONS

    current_pace = analysis_store.load(session_path, "pace")
    prior_session = find_prior_session(session_path)
    prior_pecking = (
        analysis_store.load(prior_session, "pecking_order") if prior_session else None
    )
    # Per SME: tyre compounds are PER-RACE (FIA picks 3 of C1-C6 each
    # event; the "MEDIUM" at race A is different rubber than at race B).
    # So race_pace_by_compound MUST NOT carry across events even though
    # the overall quali/race team-rank predictions DO chain across.
    prior_session_same_event = (
        prior_session is not None
        and prior_session.parent == session_path.parent
    )

    cohorts_prev_event = load_previous_event_cohorts(session_path)

    # Build a per-team color lookup from whatever is available.
    color_lookup: dict[str, str] = {}
    for src in (current_pace or {}).get("quali_pace", []):
        if src.get("team"): color_lookup[src["team"]] = src.get("color") or "#888"
    for src in (current_pace or {}).get("race_pace", []):
        if src.get("team"): color_lookup.setdefault(src["team"], src.get("color") or "#888")

    quali_out = _build_metric_order(
        prior_entries=(prior_pecking or {}).get("quali_pecking_order") or [],
        current_entries=(current_pace or {}).get("quali_pace") or [],
        session_weight=session_weight,
        cohorts_prev_event=cohorts_prev_event,
        is_competitive=is_competitive,
        color_lookup=color_lookup,
        prior_session_same_event=prior_session_same_event,
    )
    race_out = _build_metric_order(
        prior_entries=(prior_pecking or {}).get("race_pecking_order") or [],
        current_entries=(current_pace or {}).get("race_pace") or [],
        session_weight=session_weight,
        cohorts_prev_event=cohorts_prev_event,
        is_competitive=is_competitive,
        color_lookup=color_lookup,
        prior_session_same_event=prior_session_same_event,
    )

    final_cohorts = {e["team"]: e["cohort"]
                     for e in race_out + quali_out
                     if e.get("cohort")}

    # Per-compound breakdown: chains WITHIN an event only — never across
    # events (= different rubber under the same label name).
    prior_compound = (
        (prior_pecking or {}).get("race_pace_by_compound") or []
        if prior_session_same_event else []
    )
    compound_out = _build_compound_breakdown(
        prior_compound=prior_compound,
        current_compound=(current_pace or {}).get("race_pace_by_compound") or [],
        color_lookup=color_lookup,
        final_cohorts=final_cohorts,
    )

    if not quali_out and not race_out:
        return None

    return {
        "session_type": canon,
        "session_name": session_path.name,
        "prior_session": prior_session.name if prior_session else None,
        "cohort_source_event": (
            analysis_store.previous_event_dir(session_path).name
            if analysis_store.previous_event_dir(session_path) else None
        ),
        "cohort_thresholds": {
            "lead_max_gap_s": COHORT_LEAD_MAX_GAP_S,
            "back_min_gap_s": COHORT_BACK_MIN_GAP_S,
        },
        "quali_pecking_order": quali_out,
        "race_pecking_order": race_out,
        "race_pace_by_compound": compound_out,
    }


def compute_and_save(session_path: Path) -> Optional[Path]:
    result = compute(session_path)
    if not result:
        return None
    return analysis_store.save(session_path, "pecking_order", result)
