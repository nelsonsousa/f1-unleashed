"""Settings API (card 27) — read/update the user settings store.

GET  /api/v1/settings        → current settings (defaults merged with on-disk)
PUT  /api/v1/settings        → deep-merge updates, persist, return the lot

The frontend settings dialog uses these; the rest of the app reads via
app.settings.get(...). Note: `cacheDir` and a few capture toggles only take
effect on the next session/restart (the active process keeps its paths).
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from app import settings as settings_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings")
async def get_settings() -> dict:
    return settings_store.load()


@router.put("/settings")
async def update_settings(updates: dict[str, Any]) -> dict:
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        return settings_store.save(updates)
    except Exception as e:
        logger.exception("Failed to save settings")
        raise HTTPException(status_code=500, detail=str(e))
