"""User settings store (card 27) — replaces .env.

All runtime configuration lives in a single JSON file under the OS-appropriate
data dir (the same location used for the cache), loaded once with defaults
applied. The settings dialog reads/writes it via /api/v1/settings. `.env` and
python-dotenv are no longer used — defaults cover every value out of the box.

The data-home location (where settings.json + the default cache live) is fixed
per-OS; the `cacheDir` setting can redirect the livetiming cache / tmp / analysis
elsewhere without moving settings.json.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Gitignored per-checkout instance overrides (run two servers side by side).
# KEY=VALUE lines; defaults = the current single-instance values. An actual
# environment variable of the same name always wins over the file.
_INSTANCE_FILE = Path(__file__).resolve().parent.parent / "instance.env"


def _parse_instance_file(text: str, key: str) -> Optional[str]:
    """Return the value for KEY in an instance.env-style body, or None."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip('"').strip("'") or None
    return None


def _instance_override(key: str) -> Optional[str]:
    """Instance override for KEY: real env var first, else the instance.env file."""
    env = os.environ.get(key)
    if env:
        return env
    try:
        return _parse_instance_file(_INSTANCE_FILE.read_text(), key)
    except OSError:
        return None


def instance_port(default: int = 1950) -> int:
    """Port this instance serves on (PORT override, else default)."""
    val = _instance_override("PORT")
    try:
        return int(val) if val else default
    except ValueError:
        return default


def _os_data_home() -> Path:
    """OS-appropriate data dir (matches the pre-card-27 DATA_DIR default).

    An F1_DATA_HOME instance override (env or instance.env) redirects the WHOLE
    data home — settings.json, cache, tmp, analysis, telemetry — so a second
    instance can run without sharing any data with the first."""
    override = _instance_override("F1_DATA_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
            or str(Path.home() / "AppData" / "Local")
        return Path(base) / "F1Unleashed"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "F1Unleashed"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "f1unleashed"


DATA_HOME = _os_data_home()
SETTINGS_FILE = DATA_HOME / "settings.json"

SESSION_TYPES = ["practice", "qualifying", "race"]


def _per_type(default: bool) -> dict:
    return {t: default for t in SESSION_TYPES}


# Every setting + its default. Nested groups mirror the dialog's sections.
DEFAULTS: dict[str, Any] = {
    "debug": False,                       # keep transient/ephemeral artefacts
    "telemetry": False,                   # opt-in diagnostic telemetry sink (audio-pause hunt)
    "cacheDir": "",                       # "" → OS default data home
    "rainbowAiApiKey": "",                # weather-radar overlay
    # Per-session-type capture toggles (practice / qualifying / race).
    "audio": _per_type(True),             # download + play commentary
    "teamRadio": _per_type(True),         # download team radio clips
    "keepFiles": _per_type(True),         # keep downloaded files after the session
    "teamRadioAutoplay": False,           # auto-play radio (else play on demand)
    "ntfy": {
        "webhookUrl": "",                 # ntfy / Discord / Slack / generic
        "sessionLive": True,              # notify when a session goes live
        "preSession": True,               # notify before a session
        "preSessionLeadMinutes": 60,      # how early
        "tokenExpiry": True,              # F1 token-expiry warnings
        "repeat": False,                  # repeat notifications
    },
    "alerts": {
        "favouriteDrivers": [],           # TLAs or car numbers
        "favouriteTeams": [],             # team names
    },
    "auth": {
        "expiryWarningHours": 24.0,
        "expiryCheckIntervalSeconds": 3600,
    },
}

_cache: Optional[dict] = None


def _deep_merge(base: dict, override: Any) -> dict:
    out = dict(base)
    if isinstance(override, dict):
        for k, v in override.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _deep_merge(out[k], v)
            else:
                out[k] = v
    return out


def load() -> dict:
    """Settings (defaults merged with the on-disk file). Cached."""
    global _cache
    if _cache is not None:
        return _cache
    data: Any = {}
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"settings load failed ({e}); using defaults")
    _cache = _deep_merge(DEFAULTS, data)
    return _cache


def get(path: str, default: Any = None) -> Any:
    """Dotted-path getter, e.g. get('ntfy.webhookUrl')."""
    cur: Any = load()
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


def save(updates: dict) -> dict:
    """Deep-merge `updates` into the current settings, persist, return the lot."""
    global _cache
    merged = _deep_merge(load(), updates or {})
    _cache = merged
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(merged, indent=2))
    except OSError as e:
        logger.error(f"settings save failed: {e}")
    return merged
