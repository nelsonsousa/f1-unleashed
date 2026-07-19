"""Telemetry API (card VOPkIiAh) — receive the client audio timeline + report
status. Opt-in: posting requires the `telemetry` setting on (default off).
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.services import telemetry

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_BYTES = 8 * 1024 * 1024   # 8 MB cap on a posted timeline


@router.get("/telemetry/status")
async def telemetry_status() -> dict:
    return {"enabled": telemetry.enabled(), "dir": str(telemetry.TELEMETRY_DIR)}


@router.post("/telemetry/audio-timeline")
async def post_audio_timeline(session: str, request: Request) -> dict:
    """Persist a client-posted audio timeline (JSON) into the telemetry subfolder."""
    if not telemetry.enabled():
        raise HTTPException(status_code=403, detail="telemetry disabled (enable the 'telemetry' setting)")
    body = await request.body()
    if len(body) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="timeline too large")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON")
    path = telemetry.save_client_timeline(session, payload)
    logger.info("telemetry: saved client timeline %s", path.name)
    return {"saved": path.name}
