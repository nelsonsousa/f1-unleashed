"""Central runtime configuration, derived from the user settings store (card 27).

Settings come from app.settings (a JSON file with defaults) rather than .env —
the app works out of the box and is configured via the in-app settings dialog.
F1 auth is NOT configured here — the F1 site has anti-robot measures, so login
is always manual.
"""

from pathlib import Path

from app import settings as _settings

# Keep the transient scratch DB (and other ephemeral processing/analysis
# artefacts) after use, for inspection, instead of deleting them. Default off.
REPLAY_DEBUG = bool(_settings.get("debug", False))


# ── Data locations (cards 25 + 27) ──────────────────────────────────────────
# DATA_DIR is the FIXED OS-appropriate data home: settings.json, known_topics.json,
# rainbow_usage.json, the transient tmp DBs, analysis output and the weather-radar
# cache all live here and never move. Only the livetiming cache is relocatable —
# the `cacheDir` setting points DIRECTLY at the cache root (it contains the season
# folders 2026/, 2025/, … with no extra "livetiming_cache" level); empty → the
# default <data home>/livetiming_cache. Existing files are moved on change via the
# settings cache-location endpoint, not automatically here.
DATA_DIR = _settings.DATA_HOME


def _cache_dir() -> Path:
    override = _settings.get("cacheDir", "")
    if override:
        return Path(override).expanduser()
    return DATA_DIR / "livetiming_cache"


CACHE_DIR = _cache_dir()                     # downloaded live.jsonl / subscribe.json per session
TMP_DIR = DATA_DIR / "tmp"                   # transient per-session scratch DBs
