"""Session-level analysis result storage.

Each session writes its analysis files under:
  data/analysis/{year}/{event}/{session}/{analysis_type}.json

This directory mirrors ``data/livetiming_cache/`` but holds derived
results (pace estimates, cohort labels, pace predictions, …) that are
useful to FUTURE sessions. The split has two purposes:

  1. Analysis results SURVIVE deletes of livetiming_cache — only a
     re-download of the raw F1 data requires re-running the full
     preprocessor; re-running analyses only needs the session.db.
  2. Analysis is read-only for the session that produced it: the
     CURRENT session does not consume its own analysis output. Only
     LATER sessions/events do.

The file format is JSON for now (small, human-readable, simple to
read/write). Switch to sqlite per-session if/when an analysis grows
large enough to need indexed queries.

Ordering across sessions is by folder-name sort, the same as
livetiming_cache. Past/present/future is unambiguous from the path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from app.config import CACHE_DIR, DATA_DIR

logger = logging.getLogger(__name__)

# Analysis output lives at the FIXED data home (card 27) — it never follows a
# relocated cacheDir. Path: <data home>/analysis/{year}/{event}/{session}/.
ANALYSIS_DIR = DATA_DIR / "analysis"


def _rel_parts(livetiming_session_path: Path) -> tuple:
    """The (year, event, session, …) tail of a session path relative to the
    cache root. Tolerates a legacy "livetiming_cache" component in the path."""
    p = Path(livetiming_session_path)
    try:
        return p.relative_to(CACHE_DIR).parts
    except ValueError:
        pass
    parts = p.parts
    if "livetiming_cache" in parts:
        return parts[parts.index("livetiming_cache") + 1:]
    raise ValueError(f"Path {p} is not under the cache root {CACHE_DIR}")


def session_dir(livetiming_session_path: Path) -> Path:
    """<data home>/analysis/{year}/{event}/{session}/ for the given session."""
    return ANALYSIS_DIR.joinpath(*_rel_parts(livetiming_session_path))


def save(livetiming_session_path: Path, analysis_type: str, data: Any) -> Path:
    """Write ``data`` as JSON to {session_dir}/{analysis_type}.json.
    Creates parent directories as needed. Returns the written path.
    """
    out_dir = session_dir(livetiming_session_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{analysis_type}.json"
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return out_file


def load(livetiming_session_path: Path, analysis_type: str) -> Optional[Any]:
    """Read {session_dir}/{analysis_type}.json. Returns None if missing
    or unreadable."""
    out_file = session_dir(livetiming_session_path) / f"{analysis_type}.json"
    if not out_file.exists():
        return None
    try:
        with open(out_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to load %s", out_file)
        return None


def season_dir(year: int) -> Path:
    """<data home>/analysis/{year}/ — season-scoped analysis output."""
    return ANALYSIS_DIR / str(year)


def save_season(year: int, analysis_type: str, data: Any) -> Path:
    """Write ``data`` as JSON to {season_dir}/{analysis_type}.json.
    Creates parent directories as needed. Returns the written path."""
    out_dir = season_dir(year)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"{analysis_type}.json"
    with open(out_file, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return out_file


def load_season(year: int, analysis_type: str) -> Optional[Any]:
    """Read {season_dir}/{analysis_type}.json. Returns None if missing
    or unreadable."""
    out_file = season_dir(year) / f"{analysis_type}.json"
    if not out_file.exists():
        return None
    try:
        with open(out_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to load %s", out_file)
        return None


def previous_event_dir(livetiming_session_path: Path) -> Optional[Path]:
    """Return <data home>/analysis/{year}/{prev_event}/ — the analysis directory
    for the event sorted immediately BEFORE the current session's event.

    Walks the analysis tree (NOT livetiming_cache), so survives cache deletes.
    Returns None if no previous event has analysis stored.
    """
    try:
        rel = _rel_parts(livetiming_session_path)
    except ValueError:
        return None
    if len(rel) < 2:
        return None
    year, cur_event = rel[0], rel[1]
    analysis_year = ANALYSIS_DIR / year
    if not analysis_year.is_dir():
        return None
    prev = [
        p for p in sorted(analysis_year.iterdir())
        if p.is_dir() and p.name < cur_event
    ]
    return prev[-1] if prev else None
