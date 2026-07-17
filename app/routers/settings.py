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
import asyncio
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


@router.get("/settings")
async def get_settings() -> dict:
    s = dict(settings_store.load())
    s["_cacheDir"] = str(config.CACHE_DIR)            # effective livetiming cache root
    s["_dataHome"] = str(settings_store.DATA_HOME)    # fixed OS data home
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
    # The native dialog blocks until the user closes it — off the event loop (H4).
    return {"path": (await asyncio.to_thread(_native_pick_folder)) or ""}


def _move_cache_contents(old: Path, new: Path) -> list[str]:
    """Move the cache root's CONTENTS (season folders 2026/, 2025/, …) directly into
    `new` — no wrapper level. Blocking (copytree/rmtree/move) — call via
    asyncio.to_thread (H4)."""
    moved: list[str] = []
    new.mkdir(parents=True, exist_ok=True)
    # Cross-device moves are a real copy, so verify the destination has room before
    # touching anything. Same-device moves are renames (no extra space needed). (M4)
    if old.stat().st_dev != new.stat().st_dev:
        need = sum(f.stat().st_size for f in old.rglob("*") if f.is_file())
        free = shutil.disk_usage(new).free
        if need > free:
            raise OSError(
                f"not enough free space at destination: need {need:,} bytes, {free:,} free")
    for child in list(old.iterdir()):
        dst = new / child.name
        if dst.exists():
            if child.is_dir():
                shutil.copytree(child, dst, dirs_exist_ok=True)
                shutil.rmtree(child)
            else:
                shutil.copy2(child, dst)
                child.unlink()
        else:
            shutil.move(str(child), str(dst))
        moved.append(child.name)
    return moved


@router.post("/settings/cache-location")
async def set_cache_location(body: dict) -> dict:
    new_path = (body or {}).get("path", "")
    do_move = bool((body or {}).get("move", False))
    if not isinstance(new_path, str) or not new_path.strip():
        raise HTTPException(status_code=400, detail="path required")
    new = Path(new_path).expanduser()
    old = config.CACHE_DIR   # only the livetiming cache moves (analysis/tmp/etc. stay)
    moved: list[str] = []

    # Reject a target that OVERLAPS the current cache (one path contains the other).
    # A move would copytree/rmtree within its own tree and could destroy the source (M4).
    new_r, old_r = new.resolve(), old.resolve()
    if new_r != old_r and (new_r.is_relative_to(old_r) or old_r.is_relative_to(new_r)):
        raise HTTPException(
            status_code=400,
            detail="cache location cannot overlap the current one (one path contains the other)")

    if do_move and old.exists() and old_r != new_r:
        try:
            # copytree/rmtree/move can be large — off the event loop (H4).
            moved = await asyncio.to_thread(_move_cache_contents, old, new)
        except OSError as e:
            logger.exception("cache move failed")
            raise HTTPException(status_code=500, detail=f"move failed: {e}")

    settings_store.save({"cacheDir": str(new)})
    return {"cacheDir": str(new), "moved": moved, "restartRequired": True}
