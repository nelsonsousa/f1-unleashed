"""Replay router for streaming session timing data."""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.replay_manager import ReplayStatus, replay_manager

logger = logging.getLogger(__name__)

router = APIRouter()


class StartReplayRequest(BaseModel):
    """Request to start a replay session."""

    year: int
    race: str
    session_type: str
    speed: float = 1.0


class StartReplayResponse(BaseModel):
    """Response when a replay is started."""

    replay_id: str
    status: str
    message: str


class ControlRequest(BaseModel):
    """Request to control replay playback."""

    action: str  # play, pause, speed, seek
    value: Optional[float] = None  # speed value or frame index


@router.post("/start", response_model=StartReplayResponse)
async def start_replay(request: StartReplayRequest):
    """
    Start a new replay session for the specified F1 session.

    The session data will be loaded in the background. Connect to the
    stream endpoint to receive timing updates.
    """
    replay = await replay_manager.start_replay(
        year=request.year,
        race=request.race,
        session_type=request.session_type,
        speed=request.speed,
    )

    return StartReplayResponse(
        replay_id=replay.replay_id,
        status=replay.status.value,
        message=f"Replay created. Stream at /api/v1/replay/{replay.replay_id}/stream",
    )


@router.get("/{replay_id}")
async def get_replay_status(replay_id: str):
    """Get the current status of a replay session."""
    replay = await replay_manager.get_replay(replay_id)
    if not replay:
        raise HTTPException(status_code=404, detail="Replay not found")

    return replay.to_dict()


@router.post("/{replay_id}/control")
async def control_replay(replay_id: str, request: ControlRequest):
    """
    Control replay playback.

    Actions:
    - play: Start or resume playback
    - pause: Pause playback
    - speed: Set playback speed (value: 0.5 to 20.0)
    - seek: Jump to specific frame (value: frame index)
    - seek_time: Seek by time delta in seconds (value: e.g., -5.0 for back 5s)
    """
    replay = await replay_manager.get_replay(replay_id)
    if not replay:
        raise HTTPException(status_code=404, detail="Replay not found")

    if request.action == "play":
        success = await replay_manager.play(replay_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot play: status is {replay.status.value}",
            )

    elif request.action == "pause":
        success = await replay_manager.pause(replay_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot pause: status is {replay.status.value}",
            )

    elif request.action == "speed":
        if request.value is None:
            raise HTTPException(status_code=400, detail="Speed value required")
        await replay_manager.set_speed(replay_id, request.value)

    elif request.action == "seek":
        if request.value is None:
            raise HTTPException(status_code=400, detail="Frame index required")
        await replay_manager.seek(replay_id, int(request.value))

    elif request.action == "seek_time":
        if request.value is None:
            raise HTTPException(status_code=400, detail="Time delta required")
        await replay_manager.seek_by_time(replay_id, request.value)

    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {request.action}")

    # Return updated status with current frame data
    return replay_manager.get_current_state(replay_id)


@router.get("/{replay_id}/stream")
async def stream_replay(replay_id: str):
    """
    Stream replay timing updates via Server-Sent Events.

    Connect to this endpoint to receive real-time timing data.
    The stream sends updates as the replay progresses.

    SSE Event Format:
    ```
    event: frame
    data: {"session_time": "00:15:32", "positions": [...], ...}

    event: done
    data: {"replay_id": "...", "status": "completed"}
    ```
    """
    replay = await replay_manager.get_replay(replay_id)
    if not replay:
        raise HTTPException(status_code=404, detail="Replay not found")

    async def event_generator():
        queue = await replay_manager.subscribe(replay_id)
        try:
            while True:
                try:
                    # Use shorter timeout to send keepalives more frequently
                    update = await asyncio.wait_for(queue.get(), timeout=15.0)

                    event_type = "done" if update.get("final") else "frame"
                    yield f"event: {event_type}\ndata: {json.dumps(update)}\n\n"

                    if update.get("final"):
                        break

                    # Also break on error status
                    if update.get("status") == "error":
                        break

                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"

        finally:
            await replay_manager.unsubscribe(replay_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{replay_id}")
async def stop_replay(replay_id: str):
    """Stop and remove a replay session."""
    replay = await replay_manager.get_replay(replay_id)
    if not replay:
        raise HTTPException(status_code=404, detail="Replay not found")

    # Pause first to stop playback
    await replay_manager.pause(replay_id)

    return {"message": "Replay stopped", "replay_id": replay_id}


@router.delete("/cleanup")
async def cleanup_replays(max_age_hours: int = 4):
    """Clean up old replay sessions."""
    removed = await replay_manager.cleanup_old_replays(max_age_hours)
    return {"removed": removed, "message": f"Removed {removed} old replays"}
