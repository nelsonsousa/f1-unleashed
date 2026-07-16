"""Raw per-lap stint dataset — the raw-material layer for a season-wide pace model.

Reads a session's transient DB (built from ``live.jsonl`` by the
SessionPreProcessor) and flattens it into a list of :class:`LapRow` — one row
per driver per lap — carrying stint / compound / tyre-age / lap-time /
lap-class / interval-to-car-ahead. Race + Sprint sessions only (those are the
long-run pace signal).

Data-access recipe (verified against the real processor emits):
  - driver list           → ``driverList``                (latest)
  - per-lap time          → ``driverLaps:{num}``          (full history)
  - per-lap class         → ``driverLapClassification:{num}`` (full history)
  - stints                → ``tyreHistory:{num}`` (past stints, latest)
                            + ``currentTyre:{num}`` (running stint, latest)
  - interval to car ahead → ``driverInt:{num}``           (full history, race)

Nothing here re-derives display state; it only reads what the pipeline emitted.
"""
from __future__ import annotations

import bisect
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.processing.database import transient_db_path
from app.processing.preprocessor import SessionPreProcessor

logger = logging.getLogger(__name__)


# Relocated from the removed tyre_phases analysis module (M2) — small shared helpers
# used by this dataset builder and (transitively) the live pecking-order path.
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


@dataclass
class LapRow:
    year: int
    event: str            # cache event dir name, e.g. "1289_Silverstone"
    session: str          # cache session dir name cleaned, e.g. "Race" / "Sprint"
    session_type: str     # "race" | "sprint"
    driver: str           # car number, e.g. "44"
    tla: str
    team: str
    stint_idx: int        # 0-based stint order
    compound: str         # SOFT/MEDIUM/HARD/INTERMEDIATE/WET
    tyre_age: int         # laps since the set was fitted; 0 = first lap on the set
    lap_number: int
    lap_time_ms: int
    lap_class: str        # "" normal / OUT / PIT / STOP / CHECKERED
    interval_ahead_s: Optional[float]   # interval to car ahead at lap completion; None if leader/lapped/unknown


# ── session-name / session-type helpers ─────────────────────────────────────

def _clean_session_name(folder_name: str) -> str:
    """"11326_Race" → "Race"; "11317_Sprint_Qualifying" → "Sprint Qualifying"."""
    parts = folder_name.split("_", 1)
    rest = parts[1] if len(parts) == 2 and parts[0].isdigit() else folder_name
    return rest.replace("_", " ")


def _session_type_for(folder_name: str) -> str:
    return "sprint" if "Sprint" in folder_name else "race"


def _is_race_or_sprint(folder_name: str) -> bool:
    name = _clean_session_name(folder_name)
    if "Race" in name:
        return True
    return "Sprint" in name and "Qualifying" not in name


# ── DB read helpers ─────────────────────────────────────────────────────────

def _latest_value(conn: sqlite3.Connection, topic: str):
    """Parsed JSON of the highest-offset row for an exact topic, or None."""
    row = conn.execute(
        "SELECT data FROM messages WHERE topic = ? ORDER BY offset_ms DESC LIMIT 1",
        (topic,),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def _lap_times(conn: sqlite3.Connection, num: str) -> dict[int, tuple[int, int]]:
    """{lap_number: (completion_offset_ms, lap_time_ms)} from driverLaps history.

    Each driverLaps emit carries the driver's current lastLap; the FIRST offset
    at which a given lap appears as lastLap is that lap's completion.
    """
    out: dict[int, tuple[int, int]] = {}
    for off, data in conn.execute(
        "SELECT offset_ms, data FROM messages WHERE topic = ? ORDER BY offset_ms",
        (f"driverLaps:{num}",),
    ):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        last = d.get("lastLap")
        if not isinstance(last, dict):
            continue
        lap = last.get("lap")
        if lap is None or lap in out:
            continue
        ms = _parse_ms(last.get("time"))
        if ms is None or ms <= 0:
            continue
        out[int(lap)] = (int(off), ms)
    return out


def _lap_classes(conn: sqlite3.Connection, num: str) -> dict[int, str]:
    """{lap_number: type} from driverLapClassification history (last type wins)."""
    out: dict[int, str] = {}
    for (data,) in conn.execute(
        "SELECT data FROM messages WHERE topic = ? ORDER BY offset_ms",
        (f"driverLapClassification:{num}",),
    ):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        lap = d.get("lap")
        if lap is None:
            continue
        out[int(lap)] = d.get("type", "") or ""
    return out


def _int_history(conn: sqlite3.Connection, num: str) -> tuple[list[int], list[Optional[float]]]:
    """(offsets, interval_seconds) parallel arrays from driverInt history,
    ordered by offset. interval is None for leader / "+1 LAP" / non-numeric."""
    offs: list[int] = []
    vals: list[Optional[float]] = []
    for off, data in conn.execute(
        "SELECT offset_ms, data FROM messages WHERE topic = ? ORDER BY offset_ms",
        (f"driverInt:{num}",),
    ):
        try:
            d = json.loads(data)
        except json.JSONDecodeError:
            continue
        offs.append(int(off))
        vals.append(_parse_interval(d.get("interval")))
    return offs, vals


def _parse_interval(s) -> Optional[float]:
    """"+0.345" → 0.345; None for blank / "+1 LAP" / anything non-numeric."""
    if not isinstance(s, str):
        return None
    t = s.strip().lstrip("+")
    if not t or "LAP" in t.upper():
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _interval_at(offs: list[int], vals: list[Optional[float]], at: int) -> Optional[float]:
    """Last interval value with offset <= `at`, or None if none precedes it."""
    if not offs:
        return None
    i = bisect.bisect_right(offs, at) - 1
    if i < 0:
        return None
    return vals[i]


def _reconstruct_stints(history, current) -> list[dict]:
    """tyreHistory (past stints) + currentTyre (running) → contiguous stint
    ranges. Returns [{stint_idx, compound, start_lap, end_lap, n_laps}, …]
    covering laps 1.., or [] if there is no running stint (no tyre data)."""
    if not isinstance(current, dict):
        return []
    stints: list[tuple[str, int]] = []
    if isinstance(history, list):
        for th in history:
            if isinstance(th, dict):
                stints.append(((th.get("compound") or "").upper(),
                               int(th.get("totalLaps") or 0)))
    stints.append(((current.get("compound") or "").upper(),
                   int(current.get("age") or 0)))

    ranges: list[dict] = []
    start = 1
    for idx, (compound, n_laps) in enumerate(stints):
        ranges.append({
            "stint_idx": idx,
            "compound": compound,
            "start_lap": start,
            "end_lap": start + n_laps - 1,
            "n_laps": n_laps,
        })
        start += n_laps
    return ranges


def _stint_for_lap(ranges: list[dict], lap: int) -> Optional[dict]:
    for r in ranges:
        if r["start_lap"] <= lap <= r["end_lap"]:
            return r
    return None


# ── extraction ──────────────────────────────────────────────────────────────

def extract_session(
    conn: sqlite3.Connection,
    year: int,
    event: str,
    session: str,
    session_type: str,
) -> list[LapRow]:
    """All drivers, one LapRow per timed lap, per the recipe above.

    Drivers with no tyre data are skipped. Laps outside the reconstructed
    stint coverage are skipped (no stint → no compound/age).
    """
    drivers = _load_driver_list(conn)
    rows: list[LapRow] = []
    for num, info in sorted(drivers.items()):
        ranges = _reconstruct_stints(
            _latest_value(conn, f"tyreHistory:{num}"),
            _latest_value(conn, f"currentTyre:{num}"),
        )
        if not ranges:
            continue  # no tyre data — skip driver
        times = _lap_times(conn, num)
        if not times:
            continue
        classes = _lap_classes(conn, num)
        int_offs, int_vals = _int_history(conn, num)

        for lap in sorted(times):
            completion_off, lap_ms = times[lap]
            stint = _stint_for_lap(ranges, lap)
            if stint is None:
                continue  # lap outside stint coverage
            rows.append(LapRow(
                year=year,
                event=event,
                session=session,
                session_type=session_type,
                driver=num,
                tla=info["tla"],
                team=info["team"],
                stint_idx=stint["stint_idx"],
                compound=stint["compound"],
                tyre_age=lap - stint["start_lap"],
                lap_number=lap,
                lap_time_ms=lap_ms,
                lap_class=classes.get(lap, ""),
                interval_ahead_s=_interval_at(int_offs, int_vals, completion_off),
            ))
    return rows


async def build_and_extract(session_path: Path, force: bool = True) -> list[LapRow]:
    """Build the transient DB, open it read-only, extract rows. force=False reuses an
    already-complete DB (fast re-runs over the cached season without rebuilding)."""
    year_s, event, session_folder = session_path.parts[-3:]
    year = int(year_s)
    session = _clean_session_name(session_folder)
    session_type = _session_type_for(session_folder)

    pre = SessionPreProcessor(session_path, "")
    try:
        await pre.run(force=force)
        db = transient_db_path(session_path)
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            return extract_session(conn, year, event, session, session_type)
        finally:
            conn.close()
    finally:
        pre.close()


async def season_dataset(year: int, force: bool = True) -> list[LapRow]:
    """Enumerate race + sprint sessions for `year` and extract every lap. force=False
    reuses cached transient DBs from a prior run (no rebuild)."""
    from app.services.cache_manager import cache_manager

    rows: list[LapRow] = []
    for event in cache_manager.get_cached_sessions():
        if event.year != year:
            continue
        for s in event.sessions:
            folder = Path(s.cache_path).name
            if not _is_race_or_sprint(folder):
                continue
            print(f"[season_dataset] {year} {event.location} {folder} …", flush=True)
            session_rows = await build_and_extract(Path(s.cache_path), force=force)
            print(f"[season_dataset]   → {len(session_rows)} laps", flush=True)
            rows.extend(session_rows)
    return rows
