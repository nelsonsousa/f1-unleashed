"""
Per-session pace analysis — team-level quali + race pace extraction.

Subscribes to driverList + per-driver lapClassification + driverLapTimes
+ driverTyres via wildcard. At preprocessor finalize, computes:

  - Quali pace per team = best PUSH lap (= the team's fastest driver's
    best PUSH-classified lap).
  - Race pace per team = v2 extraction (= top-2 compounds session-wide,
    longest stint per compound per driver, trimmed-mean per compound,
    driver pace = mean of per-compound paces, team = fastest driver).

Results are written to ``data/analysis/{year}/{event}/{session}/pace.json``
via app.processing.analysis_store. The current session does NOT emit
analysis on the message bus — pecking-order / cohort consumers in the
race-control tile use the PREVIOUS event/session's stored analysis.

Helpers:

  * load_previous_event_cohorts(session_path) — derive per-team cohort
    labels from previous event's pace.json files (pace-band thresholds
    on aggregated pace gaps across Q/SQ/S/R sessions).
"""
from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.processing import analysis_store
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor

logger = logging.getLogger(__name__)


# Pace-band cohort thresholds. A team's cohort for the CURRENT event
# is determined from the PREVIOUS event's mean pace gap (in seconds) to
# the fastest team across that event's Q/SQ/S/R sessions.
#   ≤ 0.7 s  → leaders
#   ≤ 2.5 s  → midfield
#   >  2.5 s → backmarkers
# A single event of pace-band placement is sufficient to reclassify
# (1-event rule per SME). Cohorts reflect pace-gap clusters; moving
# between cohorts means closing the gap to the next cluster.
COHORT_LEAD_MAX_GAP_S = 0.7
COHORT_BACK_MIN_GAP_S = 2.5
COHORT_LEADERS = "leaders"
COHORT_MIDFIELD = "midfield"
COHORT_BACKMARKERS = "backmarkers"


def _pace_band_cohort(gap_s: float) -> str:
    if gap_s <= COHORT_LEAD_MAX_GAP_S:
        return COHORT_LEADERS
    if gap_s > COHORT_BACK_MIN_GAP_S:
        return COHORT_BACKMARKERS
    return COHORT_MIDFIELD


def _parse_ms(s: Optional[str]) -> Optional[int]:
    """Parse '1:23.456' or '23.456' → ms."""
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


def _session_type_code(name: str) -> Optional[str]:
    """Folder-name → Q / SQ / S / R, or None for FP / unknown."""
    if "_" in name:
        head, _, rest = name.partition("_")
        if head.isdigit():
            name = rest
    if name == "Sprint_Qualifying":
        return "SQ"
    if name == "Qualifying":
        return "Q"
    if name == "Race":
        return "R"
    if name == "Sprint":
        return "S"
    return None


class PaceProcessor(Processor):
    """Per-session pace extraction. Writes pace.json at finalize; does
    not emit on the message bus."""

    def __init__(self, bus: SessionMessageBus, session_type: str,
                 session_path: Optional[Path] = None,
                 session_name: Optional[str] = None):
        super().__init__(bus, session_type)
        self._session_path = session_path
        self._session_name = session_name or session_type
        self._drivers: dict[str, dict] = {}     # num → {tla, team, color}
        self._lap_times: dict[str, dict[int, int]] = {}  # num → {lap → ms}
        self._lap_cls: dict[str, dict[int, str]] = {}    # num → {lap → status}
        # num → {lap → [s1_ms, s2_ms, s3_ms]} (= each may be None).
        self._lap_sectors: dict[str, dict[int, list[Optional[int]]]] = {}
        # num → [(start_lap, compound, length), …] from driverTyres.
        self._stints: dict[str, list[tuple[int, str, int]]] = {}

    # ── public API used by preprocessor ────────────────────────────────

    def save_analysis(self) -> Optional[Path]:
        """Compute per-team quali + race pace and persist as pace.json
        under data/analysis/{year}/{event}/{session}/. Returns the
        written path, or None if no data or no session_path."""
        if not self._session_path:
            return None
        pace = self.compute_session_pace()
        if not pace:
            return None
        try:
            return analysis_store.save(self._session_path, "pace", pace)
        except Exception:
            logger.exception("Failed to write pace analysis for %s",
                             self._session_path)
            return None

    # ── subscriptions ──────────────────────────────────────────────────

    def subscribe(self) -> None:
        self._bus.on("driverList", self._on_driver_list)
        self._bus.on("*", self._on_any)

    def _on_driver_list(self, data: Any, clock_time: datetime) -> None:
        if not isinstance(data, dict):
            return
        for num, info in data.items():
            if not isinstance(info, dict):
                continue
            self._drivers[num] = {
                "tla": info.get("tla") or num,
                "team": info.get("teamName") or "",
                "color": "#" + (info.get("teamColour") or "888888"),
            }

    def _on_any(self, topic: str, data: Any, clock_time: datetime) -> None:
        if topic.startswith("lapClassification:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict) and isinstance(data.get("laps"), dict):
                clean: dict[int, str] = {}
                for lap_str, status in data["laps"].items():
                    try:
                        clean[int(lap_str)] = status
                    except (ValueError, TypeError):
                        pass
                self._lap_cls[num] = clean
        elif topic.startswith("driverLapTimes:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, dict):
                clean: dict[int, int] = {}
                for lap_str, t in data.items():
                    try:
                        lap_num = int(lap_str)
                    except (ValueError, TypeError):
                        continue
                    ms = _parse_ms(t)
                    if ms is not None:
                        clean[lap_num] = ms
                self._lap_times[num] = clean
        elif topic.startswith("driverLastLap:"):
            # Sector times for the ideal-lap calculation in quali pace.
            num = topic.split(":", 1)[1]
            if isinstance(data, dict):
                lap = data.get("lap")
                secs = data.get("sectors") or []
                if isinstance(lap, int) and isinstance(secs, list):
                    parsed: list[Optional[int]] = []
                    for s in secs[:3]:
                        v = s.get("value") if isinstance(s, dict) else None
                        ms = None
                        if v not in (None, "", "null"):
                            try:
                                ms = int(float(v) * 1000)
                            except (ValueError, TypeError):
                                ms = None
                        parsed.append(ms)
                    while len(parsed) < 3:
                        parsed.append(None)
                    self._lap_sectors.setdefault(num, {})[lap] = parsed
        elif topic.startswith("driverTyres:"):
            num = topic.split(":", 1)[1]
            if isinstance(data, list):
                ts = sorted(data, key=lambda s: s.get("lap", 0) if isinstance(s, dict) else 0)
                stints: list[tuple[int, str, int]] = []
                for i, s in enumerate(ts):
                    if not isinstance(s, dict):
                        continue
                    start = s.get("lap")
                    if start is None:
                        continue
                    comp = s.get("compound") or ""
                    if i + 1 < len(ts):
                        nxt = ts[i + 1] if isinstance(ts[i + 1], dict) else {}
                        length = (nxt.get("lap", start) or start) - start
                    else:
                        length = (s.get("totalLaps") or 0) - (s.get("startLaps") or 0)
                    if length > 0:
                        stints.append((start, comp, length))
                self._stints[num] = stints

    # ── pace computation ───────────────────────────────────────────────

    def _driver_quali_pace(self, num: str) -> Optional[tuple[int, float]]:
        """Driver's ideal lap = sum of best per-sector times across the
        baseline lap set. SME 2026-06-05 spec:

        1. Take all PUSH ∪ LONG laps as candidates.
        2. Filter to those within 2 % of the driver's fastest valid lap.
        3. For each of the 3 sectors, take the minimum across the filtered
           set. Sum = the driver's ideal lap, i.e. their pace under
           perfect conditions.
        4. If sector data is incomplete, fall back to the fastest filtered
           lap; if no PUSH/LONG laps exist, fall back to any timed lap
           that isn't OUT/IN/PIT.

        Returns (lap_ms, confidence) where confidence rises with the
        number of baseline laps available (capped at 1.0 with ≥ 3 laps).
        """
        lt = self._lap_times.get(num, {})
        cls = self._lap_cls.get(num, {})
        secs = self._lap_sectors.get(num, {})

        # 1. Candidate set: PUSH ∪ LONG with a recorded lap time.
        candidates = [(lap, lt[lap]) for lap, s in cls.items()
                      if s in ("PUSH", "LONG") and lap in lt]
        if not candidates:
            # Final fallback: any timed lap not OUT/IN/PIT/STOP.
            timed = [t for lap, t in lt.items()
                     if cls.get(lap, "") not in ("OUT", "IN", "PIT", "STOP")]
            if not timed:
                return None
            return (min(timed), min(1.0, len(timed) / 3.0))

        # 2. Filter to within 2 % of fastest candidate.
        fastest = min(t for _, t in candidates)
        kept = [(lap, t) for lap, t in candidates if t <= fastest * 1.02]
        if not kept:
            return (fastest, min(1.0, len(candidates) / 3.0))

        # 3. Per-sector minimum across kept laps.
        s1s = [secs.get(lap, [None, None, None])[0] for lap, _ in kept]
        s2s = [secs.get(lap, [None, None, None])[1] for lap, _ in kept]
        s3s = [secs.get(lap, [None, None, None])[2] for lap, _ in kept]
        s1s = [x for x in s1s if x is not None]
        s2s = [x for x in s2s if x is not None]
        s3s = [x for x in s3s if x is not None]
        if s1s and s2s and s3s:
            ideal = min(s1s) + min(s2s) + min(s3s)
            return (ideal, min(1.0, len(kept) / 3.0))

        # 4. Sector data incomplete — fall back to fastest filtered lap.
        return (fastest, min(1.0, len(kept) / 3.0))

    def _compute_race_pace_all_drivers(self) -> dict[str, dict]:
        """v2 race-pace: top-2 compounds session-wide; per driver, longest
        stint per compound (≥3 timed laps, OUT/IN/PIT excluded); trim
        2% outliers; per-compound mean; driver pace = mean across compounds.

        Returns:
          {num: {
            "lap_ms": int,         # mean across compounds (= aggregate)
            "confidence": float,   # mean stint length / 5, capped at 1.0
            "by_compound": {
              "HARD": {"lap_ms": int, "n_laps": int,
                       "stint_start_lap": int, "stint_length": int},
              ...
            }
          }}
        Drivers without any qualifying stint on the top-2 compounds are
        omitted entirely.
        """
        compound_laps: dict[str, int] = {}
        for num in self._drivers:
            lt = self._lap_times.get(num, {})
            lc = self._lap_cls.get(num, {})
            for start, comp, length in self._stints.get(num, []):
                for ln in range(start, start + length):
                    if lt.get(ln) and lc.get(ln, "") not in ("OUT", "IN", "PIT"):
                        compound_laps[comp] = compound_laps.get(comp, 0) + 1
        if not compound_laps:
            return {}
        top2 = sorted(compound_laps.items(), key=lambda x: -x[1])[:2]
        top_set = {c for c, _ in top2}

        out: dict[str, dict] = {}
        for num in self._drivers:
            lt = self._lap_times.get(num, {})
            lc = self._lap_cls.get(num, {})
            by_comp: dict[str, dict] = {}
            for start, comp, length in self._stints.get(num, []):
                if comp not in top_set:
                    continue
                # Build contiguous segments of race-pace laps. Per SME
                # 2026-06-05: only LONG/RACE/WET laps represent actual
                # race-pace running; PUSH (= single quali attempts) and
                # COOL (= cool-down) distort the median and drag the
                # trimmed mean upward. Filter them out before segmenting.
                cur: list[tuple[int, int]] = []
                segments: list[list[tuple[int, int]]] = []
                race_pace_classes = ("LONG", "RACE", "WET")
                for ln in range(start, start + length):
                    t = lt.get(ln)
                    cls = lc.get(ln, "")
                    if t is None or cls not in race_pace_classes:
                        if len(cur) >= 3:
                            segments.append(cur)
                        cur = []
                        continue
                    cur.append((ln, t))
                if len(cur) >= 2:
                    segments.append(cur)
                if not segments:
                    continue
                # Aggregate ACROSS segments of this compound on this stint:
                # weight each segment's neighbour-filtered mean by its
                # length so longer stints dominate (= SME directive).
                stint_sum_w = 0
                stint_sum_w_pace = 0.0
                stint_total_kept = 0
                stint_total_len = 0
                stint_starts: list[int] = []
                stint_ends: list[int] = []
                for seg in segments:
                    # Neighbour-filter: drop any lap slower than both
                    # neighbours by ≥ 0.5 s (= one-off bad laps from
                    # traffic, error, etc.).
                    kept_times: list[int] = []
                    for i, (_, t) in enumerate(seg):
                        if 0 < i < len(seg) - 1:
                            prev_t = seg[i - 1][1]
                            next_t = seg[i + 1][1]
                            if t > prev_t + 500 and t > next_t + 500:
                                continue
                        kept_times.append(t)
                    if len(kept_times) < 2:
                        continue
                    seg_mean = statistics.mean(kept_times)
                    seg_w = len(seg)
                    stint_sum_w_pace += seg_mean * seg_w
                    stint_sum_w += seg_w
                    stint_total_kept += len(kept_times)
                    stint_total_len += seg_w
                    stint_starts.append(seg[0][0])
                    stint_ends.append(seg[-1][0])
                if stint_sum_w == 0:
                    continue
                avg_ms = int(stint_sum_w_pace / stint_sum_w)
                # Pick the LONGEST stint per compound (= keep multi-stop
                # drivers' canonical race-pace stint, not a brief reset).
                if comp in by_comp and stint_total_len <= by_comp[comp]["n_raw"]:
                    continue
                by_comp[comp] = {
                    "avg_ms": avg_ms,
                    "n_raw": stint_total_len,
                    "n_kept": stint_total_kept,
                    "stint_start_lap": min(stint_starts),
                    "stint_end_lap": max(stint_ends),
                }
            if not by_comp:
                continue
            # Driver-level race pace = mean across compounds (= summarises
            # the driver's average across the rubber they ran).
            avg_lap_ms = int(sum(v["avg_ms"] for v in by_comp.values()) / len(by_comp))
            avg_n = sum(v["n_raw"] for v in by_comp.values()) / len(by_comp)
            out[num] = {
                "lap_ms": avg_lap_ms,
                "confidence": min(1.0, avg_n / 5.0),
                "by_compound": {
                    c: {
                        "lap_ms": v["avg_ms"],
                        "n_laps": v["n_kept"],
                        "stint_start_lap": v["stint_start_lap"],
                        "stint_length": v["n_raw"],
                    }
                    for c, v in by_comp.items()
                },
            }
        return out

    def _team_aggregate(self) -> dict[str, dict]:
        """Group drivers by team; per-team pace = fastest driver per metric.
        Also aggregates per-compound race pace per team (= fastest driver
        per compound, regardless of who is fastest overall)."""
        race_pace_per_driver = self._compute_race_pace_all_drivers()
        teams: dict[str, dict] = {}
        for num, d in self._drivers.items():
            team = d["team"] or f"#{num}"
            t = teams.setdefault(team, {
                "color": d["color"],
                "q_ms": None, "q_conf": 0.0, "q_tla": None,
                "r_ms": None, "r_conf": 0.0, "r_tla": None,
                # Per-compound race pace: {compound: {driver_tla, lap_ms,
                # n_laps, stint_start_lap, stint_length}} — fastest driver
                # on that compound for this team.
                "r_by_compound": {},
            })
            qp = self._driver_quali_pace(num)
            if qp and (t["q_ms"] is None or qp[0] < t["q_ms"]):
                t["q_ms"] = qp[0]
                t["q_tla"] = d["tla"]
            if qp:
                t["q_conf"] = max(t["q_conf"], qp[1])
            rp = race_pace_per_driver.get(num)
            if rp:
                if t["r_ms"] is None or rp["lap_ms"] < t["r_ms"]:
                    t["r_ms"] = rp["lap_ms"]
                    t["r_tla"] = d["tla"]
                t["r_conf"] = max(t["r_conf"], rp["confidence"])
                for comp, comp_data in rp["by_compound"].items():
                    prev = t["r_by_compound"].get(comp)
                    if prev is None or comp_data["lap_ms"] < prev["lap_ms"]:
                        t["r_by_compound"][comp] = {
                            "driver_tla": d["tla"],
                            "lap_ms": comp_data["lap_ms"],
                            "n_laps": comp_data["n_laps"],
                            "stint_start_lap": comp_data["stint_start_lap"],
                            "stint_length": comp_data["stint_length"],
                        }
        return teams

    def compute_session_pace(self) -> Optional[dict]:
        """Build per-team quali + race pace lists for THIS session.

        Returns:
          {
            "session_type": str,
            "session_name": str,
            "quali_pace": [{rank, team, color, fastest_driver_tla,
                            lap_ms, lap_time, gap_s, confidence}, …],
            "race_pace":  [{...}, …],
          }
        Either list may be empty (e.g. quali_pace empty in pure-race
        sessions if no PUSH/timed lap qualifies; race_pace empty in pure-
        qualifying sessions if no compound stint qualifies).
        """
        teams = self._team_aggregate()
        if not teams:
            return None

        def make_list(key_ms: str, key_conf: str, key_tla: str) -> list[dict]:
            ranked = sorted(
                ((t, info) for t, info in teams.items() if info[key_ms] is not None),
                key=lambda x: x[1][key_ms],
            )
            if not ranked:
                return []
            fastest_ms = ranked[0][1][key_ms]
            return [
                {
                    "rank": i + 1,
                    "team": t,
                    "color": info["color"],
                    "fastest_driver_tla": info[key_tla],
                    "lap_ms": int(info[key_ms]),
                    "lap_time": _format_ms(int(info[key_ms])),
                    "gap_s": round((info[key_ms] - fastest_ms) / 1000.0, 3),
                    "confidence": round(info[key_conf], 2),
                }
                for i, (t, info) in enumerate(ranked)
            ]

        # Per-team per-compound race pace breakdown. Each entry has the
        # team's BEST driver on each compound (which may differ between
        # compounds — e.g. car 1 fastest on HARD, car 30 fastest on MEDIUM).
        # n_laps is the kept-after-trim count from the longest stint on
        # that compound.
        race_pace_by_compound = []
        for team_name, info in teams.items():
            if not info["r_by_compound"]:
                continue
            entry = {
                "team": team_name,
                "color": info["color"],
                "compounds": {
                    comp: {
                        "driver_tla": v["driver_tla"],
                        "lap_ms": v["lap_ms"],
                        "lap_time": _format_ms(v["lap_ms"]),
                        "n_laps": v["n_laps"],
                        "stint_start_lap": v["stint_start_lap"],
                        "stint_length": v["stint_length"],
                    }
                    for comp, v in info["r_by_compound"].items()
                },
            }
            race_pace_by_compound.append(entry)
        # Sort by team's fastest-compound lap_ms (= same order as race_pace).
        race_pace_by_compound.sort(
            key=lambda e: min(c["lap_ms"] for c in e["compounds"].values())
        )

        return {
            "session_type": self._session_type,
            "session_name": self._session_name,
            "quali_pace": make_list("q_ms", "q_conf", "q_tla"),
            "race_pace": make_list("r_ms", "r_conf", "r_tla"),
            "race_pace_by_compound": race_pace_by_compound,
        }


# ─────────────────────────────────────────────────────────────────────
# Cross-event lookups (consumed by NEXT event's analysis)
# ─────────────────────────────────────────────────────────────────────


def load_previous_event_cohorts(session_path: Path) -> dict[str, str]:
    """Return {team: cohort_name} from the previous event's pace data.

    Aggregates each team's pace gap (in seconds) to the fastest team
    across the previous event's Q/SQ/S/R session pace.json files
    (reads from data/analysis/, NOT session.db), then applies the
    pace-band thresholds. Empty dict if no previous event analysis is
    available.
    """
    prev_event_dir = analysis_store.previous_event_dir(session_path)
    if not prev_event_dir or not prev_event_dir.is_dir():
        return {}

    gaps_per_team: dict[str, list[float]] = defaultdict(list)
    for sess in prev_event_dir.iterdir():
        if not sess.is_dir():
            continue
        stype = _session_type_code(sess.name)
        if stype not in ("Q", "SQ", "S", "R"):
            continue
        # ``sess`` is under data/analysis/; load expects a livetiming path,
        # but the JSON file is right next to ``sess``. Read directly.
        pace_file = sess / "pace.json"
        if not pace_file.exists():
            continue
        try:
            import json
            with open(pace_file) as f:
                pace = json.load(f)
        except (OSError, ValueError):
            continue
        key = "quali_pace" if stype in ("Q", "SQ") else "race_pace"
        entries = pace.get(key) or []
        for e in entries:
            team = e.get("team")
            gap = e.get("gap_s")
            if team is not None and gap is not None:
                gaps_per_team[team].append(gap)

    cohorts: dict[str, str] = {}
    for team, gaps in gaps_per_team.items():
        cohorts[team] = _pace_band_cohort(sum(gaps) / len(gaps))
    return cohorts
