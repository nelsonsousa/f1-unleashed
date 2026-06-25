"""Settings API (card 27) — read/update the user settings store.

GET  /api/v1/settings                 → settings + resolved cache paths
PUT  /api/v1/settings                 → deep-merge updates, persist, return all
POST /api/v1/settings/pick-folder     → native folder picker → {path}
POST /api/v1/settings/cache-location  → optionally move the cache, set cacheDir

The frontend settings dialog uses these; the rest of the app reads via
app.settings.get(...). cacheDir changes only take effect on the next restart (the
running process keeps the paths it resolved at import), hence the dialog's restart
prompt. settings.json itself stays at the fixed OS data home — it's the pointer to
wherever the cache lives, so it must not move.
"""

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from app import config
from app import settings as settings_store

logger = logging.getLogger(__name__)

router = APIRouter()

# Persistent cache data moved when the location changes (tmp is transient — it is
# rebuilt on demand, so it is left behind / recreated under the new location).
_MOVABLE_SUBDIRS = ("livetiming_cache", "analysis", "weather_radar_cache")


@router.get("/settings")
async def get_settings() -> dict:
    s = dict(settings_store.load())
    s["_dataDir"] = str(config.DATA_DIR)              # effective cache location
    s["_dataHome"] = str(settings_store.DATA_HOME)    # OS default
    return s


@router.put("/settings")
async def update_settings(updates: dict[str, Any]) -> dict:
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    # cacheDir is managed via /settings/cache-location (it may move files); ignore
    # it here so a generic save can't silently repoint the cache.
    updates.pop("cacheDir", None)
    updates.pop("_dataDir", None)
    updates.pop("_dataHome", None)
    try:
        return settings_store.save(updates)
    except Exception as e:
        logger.exception("Failed to save settings")
        raise HTTPException(status_code=500, detail=str(e))


def _native_pick_folder() -> Optional[str]:
    """Open the OS folder picker and return the chosen path (local app)."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["osascript", "-e",
                 'POSIX path of (choose folder with prompt "Select F1 Unleashed cache location")'],
                capture_output=True, text=True, timeout=120)
            return (out.stdout or "").strip() or None
        if sys.platform.startswith("linux"):
            out = subprocess.run(
                ["zenity", "--file-selection", "--directory",
                 "--title=Select F1 Unleashed cache location"],
                capture_output=True, text=True, timeout=120)
            return (out.stdout or "").strip() or None
        if sys.platform == "win32":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}")
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, text=True, timeout=120)
            return (out.stdout or "").strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning(f"folder picker unavailable: {e}")
    return None


@router.post("/settings/pick-folder")
async def pick_folder() -> dict:
    return {"path": _native_pick_folder() or ""}


@router.post("/settings/cache-location")
async def set_cache_location(body: dict) -> dict:
    new_path = (body or {}).get("path", "")
    do_move = bool((body or {}).get("move", False))
    if not isinstance(new_path, str) or not new_path.strip():
        raise HTTPException(status_code=400, detail="path required")
    new = Path(new_path).expanduser()
    old = config.DATA_DIR
    moved: list[str] = []

    if do_move and old.exists() and old.resolve() != new.resolve():
        try:
            new.mkdir(parents=True, exist_ok=True)
            for sub in _MOVABLE_SUBDIRS:
                src = old / sub
                if not src.exists():
                    continue
                dst = new / sub
                if dst.exists():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    shutil.rmtree(src)
                else:
                    shutil.move(str(src), str(dst))
                moved.append(sub)
        except OSError as e:
            logger.exception("cache move failed")
            raise HTTPException(status_code=500, detail=f"move failed: {e}")

    settings_store.save({"cacheDir": str(new)})
    return {"cacheDir": str(new), "moved": moved, "restartRequired": True}
