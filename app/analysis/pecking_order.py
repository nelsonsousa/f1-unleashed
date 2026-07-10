"""FP-based pecking-order prediction per session.

Each practice-type session ranks the teams for QUALIFYING and for RACE pace, refined
cumulatively across the event's sessions (recent session weighted much more). The running
prediction is stored in each session's ``pecking_order.json`` and picked up by the next
session; the serving endpoint shows the PRIOR session's file, so playing Qualifying surfaces
the FP-final quali order and playing the Race surfaces the quali-refined race order.

Per-session indicators (validated 2026-07-09, mean Spearman ρ quali 0.93 / race 0.86):
  * QUALI = each team's best single-lap push (isolated quali-sim), soft-equivalent.
  * RACE  = each team's longest consecutive run (>=3 laps), medium-equivalent sustained pace.
Compound normalization is a flat 0.5 s/step (soft-med, med-hard); no degradation term (FP
runs too short). Within a session each team is expressed as a GAP to the session's fastest so
track evolution cancels; the running blend is an EWMA (alpha=0.6) of those gaps.

Session roles in the chain:
  * Non-sprint: FP1 -> FP2 -> FP3 -> Q -> R.  Sprint: FP1 -> SQ -> S -> Q -> R.
  * FP1/2/3 update both quali and race blends. SQ updates quali (single laps only).
    Sprint updates race (it is a race). Qualifying carries both forward and additionally
    refines the RACE order with the just-observed qualifying result.
  * Each event is independent (no cross-event carry) per SME.

Output schema (pecking_order.json) — the client reads rank/team/color/gap_s/confidence:
  {
    "session_type", "session_name", "prior_session",
    "quali_pecking_order": [{"rank","team","color","gap_s","confidence"}, ...],
    "race_pecking_order":  [ ... same shape ... ],
    "_quali_blend": {team: {"gap": ms, "n": int}},   # picked up by the next session
    "_race_blend":  {team: {"gap": ms, "n": int}},
  }
"""
from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.processing import analysis_store
from app.processing.database import transient_db_path
from app.analysis.stint_dataset import (
    extract_session,
    _clean_session_name,
    _session_type_for,
)

logger = logging.getLogger(__name__)

SM_MS = 500          # soft -> medium compound step
MH_MS = 500          # medium -> hard compound step
ALPHA = 0.6          # EWMA recency weight (recent session dominates)
PUSH = {"PUSH", "LONG"}
RACE_COMPOUNDS = {"MEDIUM", "HARD"}

_NON_SPRINT_ORDER = ["Practice_1", "Practice_2", "Practice_3", "Qualifying", "Race"]
_SPRINT_ORDER = ["Practice_1", "Sprint_Qualifying", "Sprint", "Qualifying", "Race"]
_FP_SESSIONS = {"Practice_1", "Practice_2", "Practice_3"}


# ── chain / session helpers ───────────────────────────────────────────────
def _session_canonical_name(folder_name: str) -> Optional[str]:
    """Strip leading session-id digits: '11234_Race' -> 'Race'."""
    if "_" in folder_name:
        head, _, rest = folder_name.partition("_")
        if head.isdigit():
            return rest
    return folder_name


def _is_sprint_event(event_dir: Path) -> bool:
    for s in event_dir.iterdir():
        if s.is_dir() and _session_canonical_name(s.name) == "Sprint_Qualifying":
            return True
    return False


def _ordered_sessions_in_event(event_dir: Path) -> list[Path]:
    """Event's session folders in canonical chain order."""
    order = _SPRINT_ORDER if _is_sprint_event(event_dir) else _NON_SPRINT_ORDER
    by_canon: dict[str, Path] = {}
    for s in event_dir.iterdir():
        if s.is_dir() and _session_canonical_name(s.name) in order:
            by_canon[_session_canonical_name(s.name)] = s
    return [by_canon[c] for c in order if c in by_canon]


def find_prior_session(session_path: Path) -> Optional[Path]:
    """Session immediately before ``session_path`` in the chain; falls back to the
    previous event's last session if this is the event's first session."""
    event_dir = session_path.parent
    year_dir = event_dir.parent
    chain = _ordered_sessions_in_event(event_dir)
    try:
        idx = chain.index(session_path)
    except ValueError:
        return None
    if idx > 0:
        return chain[idx - 1]
    events = sorted((p for p in year_dir.iterdir() if p.is_dir()), key=lambda p: p.name)
    try:
        eidx = events.index(event_dir)
    except ValueError:
        return None
    if eidx == 0:
        return None
    prev_chain = _ordered_sessions_in_event(events[eidx - 1])
    return prev_chain[-1] if prev_chain else None


# ── data loading ──────────────────────────────────────────────────────────
def _load_session(session_path: Path):
    """(LapRow rows, {team: color}) read from the session's transient DB."""
    db = transient_db_path(session_path)
    if not db.exists():
        return [], {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        year_s, event, folder = session_path.parts[-3:]
        rows = extract_session(conn, int(year_s), event,
                               _clean_session_name(folder), _session_type_for(folder))
        colors: dict[str, str] = {}
        for (data,) in conn.execute("SELECT data FROM messages WHERE topic='driverList'"):
            try:
                d = json.loads(data)
            except json.JSONDecodeError:
                continue
            for info in d.values():
                if isinstance(info, dict):
                    team = (info.get("teamName") or "").strip()
                    if team and info.get("color"):
                        colors[team] = info["color"]
    finally:
        conn.close()
    return rows, colors


# ── indicators ────────────────────────────────────────────────────────────
def _cmp_soft(c):
    return {"SOFT": 0, "MEDIUM": -SM_MS, "HARD": -(SM_MS + MH_MS)}.get(c, -SM_MS)


def _cmp_med(c):
    return {"SOFT": SM_MS, "MEDIUM": 0, "HARD": -MH_MS}.get(c, 0)


def _detect_runs(rows: list, min_len: int = 1) -> list[list]:
    by_stint: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_stint[(r.driver, r.stint_idx)].append(r)
    runs: list[list] = []
    for laps in by_stint.values():
        laps.sort(key=lambda l: l.lap_number)
        cur: list = []
        prev = None
        for l in laps:
            if l.lap_class not in PUSH:
                if len(cur) >= min_len:
                    runs.append(cur)
                cur, prev = [], None
                continue
            if prev is not None and l.lap_number != prev + 1:
                if len(cur) >= min_len:
                    runs.append(cur)
                cur = []
            cur.append(l)
            prev = l.lap_number
        if len(cur) >= min_len:
            runs.append(cur)
    return runs


def _trimmed(vals):
    v = sorted(vals)
    n = len(v)
    med = v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2
    devs = sorted(abs(x - med) for x in v)
    mad = devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2
    thresh = max(3.0 * 1.4826 * mad, 500)
    kept = [x for x in v if abs(x - med) <= thresh]
    return sum(kept) / len(kept)


def _quali_indicator(rows: list) -> dict:
    singles: dict[str, list] = defaultdict(list)
    for run in _detect_runs(rows, 1):
        if len(run) == 1 and run[0].team:
            l = run[0]
            singles[l.team].append(l.lap_time_ms + _cmp_soft(l.compound))
    return {t: min(v) for t, v in singles.items()}


def _race_indicator(rows: list) -> dict:
    runs: dict[str, list] = defaultdict(list)
    for run in _detect_runs(rows, 1):
        if len(run) >= 3 and run[0].team:
            runs[run[0].team].append(run)
    out = {}
    for team, rr in runs.items():
        pref = [r for r in rr if r[0].compound in RACE_COMPOUNDS] or rr
        best = min(pref, key=lambda r: _trimmed([l.lap_time_ms + _cmp_med(l.compound)
                                                 for l in r]))
        out[team] = _trimmed([l.lap_time_ms + _cmp_med(l.compound) for l in best])
    return out


def _race_pace(rows: list) -> dict:
    """Race/Sprint pace: best representative driver's median clean green lap (compound-norm)."""
    green: dict[str, list] = defaultdict(list)
    stint0_end: dict[str, int] = defaultdict(int)
    team_of: dict[str, str] = {}
    for l in rows:
        if not l.team:
            continue
        team_of[l.driver] = l.team
        if l.stint_idx == 0:
            stint0_end[l.driver] = max(stint0_end[l.driver], l.lap_number)
        if l.lap_class == "" and l.lap_time_ms:
            green[l.driver].append(l.lap_time_ms + _cmp_med(l.compound))
    if not green:
        return {}
    field_max = max(len(v) for v in green.values())
    team_pace: dict[str, list] = defaultdict(list)
    for drv, adj in green.items():
        if len(adj) < 0.5 * field_max or stint0_end[drv] <= 3:
            continue
        best = min(adj)
        clean = [a for a in adj if a <= best + 3000]
        team_pace[team_of[drv]].append(statistics.median(clean))
    return {t: min(v) for t, v in team_pace.items() if v}


def _observed_quali(rows: list) -> dict:
    best: dict[str, int] = {}
    for l in rows:
        if l.lap_class in {"OUT", "IN", "PIT", "STOP"} or not l.lap_time_ms or not l.team:
            continue
        if l.team not in best or l.lap_time_ms < best[l.team]:
            best[l.team] = l.lap_time_ms
    return best


# ── blending / ordering ───────────────────────────────────────────────────
def _update(prev: dict, ind: dict) -> dict:
    """EWMA-update the running blend with this session's gap-to-leader indicator."""
    lead = min(ind.values())
    blend = {t: dict(v) for t, v in prev.items()}
    for t, val in ind.items():
        g = val - lead
        if t in blend:
            blend[t] = {"gap": ALPHA * g + (1 - ALPHA) * blend[t]["gap"],
                        "n": blend[t]["n"] + 1}
        else:
            blend[t] = {"gap": g, "n": 1}
    return blend


def _order(blend: dict, colors: dict) -> list:
    items = sorted(blend.items(), key=lambda kv: kv[1]["gap"])
    if not items:
        return []
    lead = items[0][1]["gap"]
    return [{"rank": i, "team": t, "color": colors.get(t, "#888"),
             "gap_s": round((b["gap"] - lead) / 1000, 3),
             "confidence": round(min(1.0, b["n"] / 3.0), 2)}
            for i, (t, b) in enumerate(items, 1)]


def _refine_race(r_blend: dict, actual_q: dict, colors: dict) -> list:
    """Blend the observed qualifying gap 50/50 with the FP race gap (both gap-to-leader,
    seconds) — lifts race accuracy and keeps the displayed gaps monotonic."""
    race_order = _order(r_blend, colors)
    if not actual_q:
        return race_order
    race_gap = {e["team"]: e["gap_s"] * 1000 for e in race_order}     # ms
    q_lead = min(actual_q.values())
    quali_gap = {t: v - q_lead for t, v in actual_q.items()}          # ms
    teams = set(race_gap) | set(quali_gap)
    refined = {}
    for t in teams:
        rg = race_gap.get(t, quali_gap.get(t))
        qg = quali_gap.get(t, race_gap.get(t))
        refined[t] = 0.5 * rg + 0.5 * qg
    items = sorted(refined.items(), key=lambda kv: kv[1])
    lead = items[0][1]
    return [{"rank": i, "team": t, "color": colors.get(t, "#888"),
             "gap_s": round((g - lead) / 1000, 3),
             "confidence": round(min(1.0, (r_blend.get(t, {}).get("n", 0) + 1) / 3.0), 2)}
            for i, (t, g) in enumerate(items, 1)]


# ── public entry points ───────────────────────────────────────────────────
def compute(session_path: Path) -> Optional[dict]:
    event_dir = session_path.parent
    chain = _ordered_sessions_in_event(event_dir)
    if session_path not in chain:
        return None
    idx = chain.index(session_path)
    canon = _session_canonical_name(session_path.name)

    rows, colors = _load_session(session_path)
    if not rows:
        return None

    prior_q, prior_r, prior_name = {}, {}, None
    if idx > 0:                                   # same-event prior only (event-scoped)
        prior = chain[idx - 1]
        prior_name = prior.name
        pj = analysis_store.load(prior, "pecking_order")
        if pj:
            prior_q = pj.get("_quali_blend", {})
            prior_r = pj.get("_race_blend", {})

    q_ind = r_ind = None
    if canon in _FP_SESSIONS:
        q_ind, r_ind = _quali_indicator(rows), _race_indicator(rows)
    elif canon == "Sprint_Qualifying":
        q_ind = _quali_indicator(rows)
    elif canon == "Sprint":
        r_ind = _race_pace(rows)

    q_blend = _update(prior_q, q_ind) if q_ind else dict(prior_q)
    r_blend = _update(prior_r, r_ind) if r_ind else dict(prior_r)

    quali_out = _order(q_blend, colors)
    if canon == "Qualifying":
        race_out = _refine_race(r_blend, _observed_quali(rows), colors)
    else:
        race_out = _order(r_blend, colors)

    if not quali_out and not race_out:
        return None
    return {
        "session_type": canon,
        "session_name": session_path.name,
        "prior_session": prior_name,
        "quali_pecking_order": quali_out,
        "race_pecking_order": race_out,
        "_quali_blend": q_blend,
        "_race_blend": r_blend,
    }


def compute_and_save(session_path: Path):
    """Compute the running pecking order for ``session_path`` and persist it."""
    result = compute(session_path)
    if result is None:
        return None
    return analysis_store.save(session_path, "pecking_order", result)
