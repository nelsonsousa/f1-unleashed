"""Empirical pit-stop time-loss measurement from completed races.

For each pit stop in a race, we estimate the time lost (vs continuing
on track) using:

  pit_loss = (in_lap_time + out_lap_time) - 2 × clean_baseline

Where:
  - in_lap = the last timed lap of the stint ending at this pit stop
    (= driver enters pit lane during this lap).
  - out_lap = the first timed lap of the new stint (= cold tyres + pit
    exit acceleration).
  - clean_baseline = median of "clean" laps in the SAME stint (not
    OUT/IN/PIT, with smoothed delta near 0 = no SC laps, no traffic).

Filters applied to identify "normal" pit stops:
  - At least 5 clean laps in the source stint (otherwise baseline noisy).
  - Skip stops in the first EARLY_STOP_MAX_LAP laps (= crashes / damage).
  - Skip stops where the IN lap is > 1.5 × baseline (= SC lap = artificially
    slow, distorts the delta).

Outputs the distribution + median/mean pit loss per session.
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


EARLY_STOP_MAX_LAP = 3
MIN_CLEAN_LAPS_FOR_BASELINE = 5
# In-lap > baseline × this → driver was in SC pace BEFORE pitting.
# Loosened from 1.20 → 1.35 so long-pit-lane tracks (Suzuka, Bahrain)
# aren't rejected (= a normal in-lap there can run +25 s vs baseline,
# = baseline × ~1.30). The plausibility window catches actual SC stops.
SC_OR_DAMAGE_RATIO = 1.35
# Out-lap > baseline × this → likely emerged behind SC.
OUT_LAP_MAX_RATIO = 1.20
# Plausibility window per SME: real green-flag pit stops cost 18-30 s
# net of track time (= varies by pit-lane length). Below 18 s = (V)SC
# opportunistic stop (= track speed reduced, so pit lane is cheap).
# Above 30 s = damage / repair / refuelling-style anomaly.
MIN_PLAUSIBLE_PIT_LOSS_S = 18
MAX_PLAUSIBLE_PIT_LOSS_S = 30


def _is_race(session_path: Path) -> bool:
    name = session_path.name
    if "_" in name:
        head, _, rest = name.partition("_")
        if head.isdigit():
            name = rest
    return name == "Race"


def _parse_ms(s):
    if not s or not isinstance(s, str):
        return None
    try:
        if ":" in s:
            m, rest = s.split(":", 1)
            sec_part, frac = (rest.split(".", 1) if "." in rest else (rest, "0"))
            return int(m) * 60_000 + int(sec_part) * 1000 + int(frac.ljust(3, "0")[:3])
        sec, frac = (s.split(".", 1) if "." in s else (s, "0"))
        return int(sec) * 1000 + int(frac.ljust(3, "0")[:3])
    except (ValueError, AttributeError):
        return None


def _load_driver_list(conn):
    drv = {}
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


def _load_latest_per_topic(conn, prefix):
    latest = {}
    for off, topic, data in conn.execute(
        f"SELECT offset_ms, topic, data FROM messages "
        f"WHERE topic LIKE '{prefix}:%' ORDER BY offset_ms"
    ):
        num = topic.split(":", 1)[1]
        if num not in latest or off > latest[num][0]:
            latest[num] = (off, data)
    out = {}
    for num, (_, data) in latest.items():
        try:
            out[num] = json.loads(data)
        except json.JSONDecodeError:
            pass
    return out


def measure_session(session_path: Path) -> Optional[dict]:
    """Measure pit losses across all pit stops in this Race session."""
    if not _is_race(session_path):
        return None
    db = session_path / "session.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        drv = _load_driver_list(conn)
        lap_times_raw = _load_latest_per_topic(conn, "driverLapTimes")
        lap_cls_raw = _load_latest_per_topic(conn, "lapClassification")
        tyres = _load_latest_per_topic(conn, "driverTyres")
    finally:
        conn.close()

    measurements = []
    for num, info in drv.items():
        # Build (lap → ms) and (lap → cls) dicts.
        lt: dict[int, int] = {}
        for k, v in (lap_times_raw.get(num) or {}).items():
            try: ln = int(k)
            except (ValueError, TypeError): continue
            ms = _parse_ms(v)
            if ms is not None and ms > 0:
                lt[ln] = ms
        lc: dict[int, str] = {}
        laps_map = (lap_cls_raw.get(num) or {}).get("laps") or {}
        for k, v in laps_map.items():
            try: lc[int(k)] = v
            except (ValueError, TypeError): pass
        ty = tyres.get(num)
        if not isinstance(ty, list) or len(ty) < 2:
            continue

        # Stints sorted by lap.
        ts = sorted((s for s in ty if isinstance(s, dict)),
                    key=lambda s: s.get("lap", 0))
        # Each transition stint[i] → stint[i+1] is a pit stop at
        # lap stint[i+1].lap. Compute pit loss per transition.
        for i in range(len(ts) - 1):
            stint_a = ts[i]
            stint_b = ts[i + 1]
            stint_a_start = stint_a.get("lap")
            stint_b_start = stint_b.get("lap")
            if stint_a_start is None or stint_b_start is None:
                continue
            if stint_b_start <= EARLY_STOP_MAX_LAP:
                continue  # crash / damage
            in_lap_num = stint_b_start - 1
            out_lap_num = stint_b_start
            in_lap = lt.get(in_lap_num)
            out_lap = lt.get(out_lap_num)
            if in_lap is None or out_lap is None:
                continue
            # Baseline = clean racing laps in stint A (not OUT/IN/PIT).
            stint_a_length = stint_b_start - stint_a_start
            clean_laps = []
            for ln in range(stint_a_start, stint_a_start + stint_a_length):
                cls = lc.get(ln, "")
                t = lt.get(ln)
                if t is None:
                    continue
                if cls in ("OUT", "IN", "PIT"):
                    continue
                clean_laps.append(t)
            if len(clean_laps) < MIN_CLEAN_LAPS_FOR_BASELINE:
                continue
            # Baseline = median of the FASTEST HALF of clean laps. If the
            # stint contains SC laps (= slow), the median over ALL clean
            # laps inflates baseline, weakening SC detection downstream.
            # Using the fastest half anchors baseline to true racing pace.
            sorted_laps = sorted(clean_laps)
            fast_half = sorted_laps[: max(MIN_CLEAN_LAPS_FOR_BASELINE,
                                          len(sorted_laps) // 2)]
            baseline = statistics.median(fast_half)
            # SC / damage filters: laps anomalously slow vs racing pace.
            if in_lap > baseline * SC_OR_DAMAGE_RATIO:
                continue
            if out_lap > baseline * OUT_LAP_MAX_RATIO:
                continue
            pit_loss_ms = (in_lap + out_lap) - 2 * baseline
            pit_loss_s = pit_loss_ms / 1000.0
            # Plausibility window: SC-affected stops register as < 10s
            # (both alternatives at SC pace); damage stops > 45s.
            if pit_loss_s < MIN_PLAUSIBLE_PIT_LOSS_S:
                continue
            if pit_loss_s > MAX_PLAUSIBLE_PIT_LOSS_S:
                continue
            measurements.append({
                "driver_tla": info["tla"],
                "team": info["team"] or f"#{num}",
                "stop_lap": stint_b_start,
                "compound_before": (stint_a.get("compound") or "").upper(),
                "compound_after": (stint_b.get("compound") or "").upper(),
                "in_lap_ms": in_lap,
                "out_lap_ms": out_lap,
                "baseline_ms": int(baseline),
                "pit_loss_s": round(pit_loss_ms / 1000.0, 2),
            })

    if not measurements:
        return None
    losses = sorted(m["pit_loss_s"] for m in measurements)
    return {
        "session_name": session_path.name,
        "n_measurements": len(measurements),
        "median_pit_loss_s": round(statistics.median(losses), 2),
        "mean_pit_loss_s": round(statistics.mean(losses), 2),
        "min_pit_loss_s": losses[0],
        "max_pit_loss_s": losses[-1],
        "p25_pit_loss_s": round(losses[len(losses) // 4], 2),
        "p75_pit_loss_s": round(losses[3 * len(losses) // 4], 2),
        "measurements": measurements,
    }


def measure_and_save(session_path: Path) -> Optional[Path]:
    r = measure_session(session_path)
    if not r:
        return None
    return analysis_store.save(session_path, "pit_loss_measurement", r)
