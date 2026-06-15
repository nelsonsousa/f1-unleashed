"""Per-session data/audio health classification (card).

Each cached session is graded grey/red/yellow/green for its data (live.jsonl)
and audio (commentary.aac) files. The verdict is computed ONCE and cached in a
``status.json`` sidecar in the session directory; it's recomputed only when
live.jsonl is newer than the sidecar (i.e. after a re-download).

Statuses:
  absent      (grey)   — the file isn't there
  corrupted   (red)    — present but unreadable / empty / missing an essential
                         aux file (subscribe.json for data, audio_info.json for audio)
  incomplete  (yellow) — present and readable but the capture was cut short
                         (no session-end marker) or a useful-but-not-critical
                         aux file is missing (e.g. pdt_map.jsonl)
  complete    (green)  — whole session captured start-to-end with its aux files

Note: deep mid-file gap detection and audio window-coverage (5 min before/after)
are NOT verified here — they need a full re-scan / ffprobe and would false-positive
on legitimate red-flag/SC pauses. Documented as a known limitation.
"""

import json
from pathlib import Path

ABSENT = "absent"
INCOMPLETE = "incomplete"
CORRUPTED = "corrupted"
COMPLETE = "complete"

_SIDECAR = "status.json"


def _first_line_is_json(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    json.loads(line)
                    return True
        return False
    except (OSError, ValueError):
        return False


def _has_session_end(path: Path) -> bool:
    """True if the tail carries F1's session-end marker (Status: Ends)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "ignore")
        return '"Status": "Ends"' in tail
    except OSError:
        return False


def _data_status(d: Path) -> tuple[str, str]:
    live = d / "live.jsonl"
    if not live.exists():
        return ABSENT, "live.jsonl not present"
    try:
        if live.stat().st_size == 0:
            return CORRUPTED, "live.jsonl is empty"
    except OSError:
        return CORRUPTED, "live.jsonl unreadable"
    if not _first_line_is_json(live):
        return CORRUPTED, "live.jsonl is not valid JSON"
    if not (d / "subscribe.json").exists():
        return CORRUPTED, "subscribe.json missing (essential aux file)"
    if not _has_session_end(live):
        return INCOMPLETE, "no session-end marker — capture cut short"
    if not (d / "pdt_map.jsonl").exists():
        return INCOMPLETE, "missing pdt_map.jsonl (audio-sync map)"
    return COMPLETE, "complete"


def _audio_status(d: Path) -> tuple[str, str]:
    aac = None
    for name in ("commentary.aac", "commentary.001.aac"):
        if (d / name).exists():
            aac = d / name
            break
    if aac is None:
        return ABSENT, "no commentary audio"
    try:
        if aac.stat().st_size == 0:
            return CORRUPTED, "commentary audio is empty"
    except OSError:
        return CORRUPTED, "commentary audio unreadable"
    if not (d / "audio_info.json").exists():
        return CORRUPTED, "audio_info.json missing (essential aux file)"
    return COMPLETE, "present"


def _compute(d: Path) -> dict:
    ds, dr = _data_status(d)
    as_, ar = _audio_status(d)
    return {
        "data_status": ds, "data_reason": dr,
        "audio_status": as_, "audio_reason": ar,
    }


def get_health(session_dir) -> dict:
    """Return the cached health verdict, recomputing only when stale."""
    d = Path(session_dir)
    sidecar = d / _SIDECAR
    live = d / "live.jsonl"
    try:
        if (sidecar.exists() and live.exists()
                and sidecar.stat().st_mtime >= live.stat().st_mtime):
            return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    verdict = _compute(d)
    try:
        sidecar.write_text(json.dumps(verdict), encoding="utf-8")
    except OSError:
        pass
    return verdict
