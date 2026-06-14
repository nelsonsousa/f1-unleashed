"""Central runtime configuration loaded from .env (see .env.example).

Loading happens here, independently of any single service, so settings are
available regardless of import order. F1 auth is NOT configured here — the
F1 site has anti-robot measures, so login is always manual.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Keep the transient scratch DB (and other ephemeral processing/analysis
# artefacts) after use, for inspection, instead of deleting them. Default off.
REPLAY_DEBUG = os.getenv("REPLAY_DEBUG", "0") == "1"


# ── Data locations (card 25) ────────────────────────────────────────────────
# The downloaded livetiming cache, derived analysis, and transient scratch DBs
# live under an OS-appropriate data directory instead of inside the repo. Set
# F1U_DATA_DIR to override (e.g. to point at an existing cache without moving
# files). Existing files are NOT migrated automatically.
def _default_data_dir() -> Path:
    override = os.getenv("F1U_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return Path(base) / "F1Unleashed"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "F1Unleashed"
    # Linux / other: XDG base dir spec.
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "f1unleashed"


DATA_DIR = _default_data_dir()
CACHE_DIR = DATA_DIR / "livetiming_cache"   # downloaded live.jsonl / subscribe.json per session
TMP_DIR = DATA_DIR / "tmp"                   # transient per-session scratch DBs
# (analysis output is derived as a sibling of the cache path by analysis_store,
# so it follows CACHE_DIR automatically — no separate constant needed.)
