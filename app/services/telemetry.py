"""Diagnostic telemetry sink (card VOPkIiAh) — for the audio-pause investigation.

Opt-in via the `telemetry` setting (default off → zero overhead, no disk writes).
Writes ONLY brand-new files into a dedicated ``<data home>/telemetry`` subfolder,
and never touches existing session/cache files — so it is safe to enable on an
instance that SHARES its data home with another running server.

- ``record(session, kind, event)`` appends one server-side event (jsonl) — e.g.
  the min(data,audio) live-edge cap decision, or an audio range-serve.
- ``save_client_timeline(session, payload)`` persists a client-posted timeline as
  a new file, so the client MSE/playback trace lands next to the server trace on
  one wall-clock timeline (align with an external speaker recording).
"""
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app import config, settings

logger = logging.getLogger(__name__)

# A NEW subfolder of the (shared) data home — never an existing dir.
TELEMETRY_DIR = config.DATA_DIR / "telemetry"


def enabled() -> bool:
    """True when the `telemetry` setting is on (default off)."""
    return bool(settings.get("telemetry", False))


def _safe(name: str) -> str:
    """Filesystem-safe token from a session name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name or "unknown")


def record(session: str, kind: str, event: dict) -> None:
    """Append one server-side telemetry event (jsonl). No-op unless enabled."""
    if not enabled():
        return
    try:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"t": int(time.time() * 1000), "kind": kind}
        rec.update(event or {})
        with open(TELEMETRY_DIR / f"{_safe(session)}.server.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        logger.debug("telemetry write failed: %s", e)


def save_client_timeline(session: str, payload: Any) -> Path:
    """Persist a client-posted timeline as a NEW file; return its path."""
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    path = TELEMETRY_DIR / f"{_safe(session)}.client.{int(time.time() * 1000)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    return path
