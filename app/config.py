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
# The downloaded livetiming cache, derived analysis, and transient scratch DBs
# live under an OS-appropriate data directory. The `cacheDir` setting overrides
# it (empty → OS default). Existing files are NOT migrated automatically.
def _default_data_dir() -> Path:
    override = _settings.get("cacheDir", "")
    if override:
        return Path(override).expanduser()
    return _settings.DATA_HOME


DATA_DIR = _default_data_dir()
CACHE_DIR = DATA_DIR / "livetiming_cache"   # downloaded live.jsonl / subscribe.json per session
TMP_DIR = DATA_DIR / "tmp"                   # transient per-session scratch DBs
# (analysis output is derived as a sibling of the cache path by analysis_store,
# so it follows CACHE_DIR automatically — no separate constant needed.)
