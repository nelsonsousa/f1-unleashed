"""
Live Timing Stream Router.

WebSocket endpoint for streaming F1 timing data with server-side processing.
"""

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.processing.session import session_manager

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/livetiming")


@router.websocket("/ws/{session_name:path}")
async def websocket_session(websocket: WebSocket, session_name: str, mode: str = ""):
    """
    WebSocket endpoint for streaming timing data with server-side processing.

    Client -> Server commands:
        { "cmd": "play" }
        { "cmd": "pause" }
        { "cmd": "seek", "offset": 1234.5 }
        { "cmd": "speed", "value": 10 }

    Server -> Client messages:
        { "topic": "state:full", "data": { ... } }        // on connect + after seek
        { "topic": "state:messages", "data": [...] }       // batch of raw messages
        { "topic": "state:clock", "data": { ... } }        // periodic clock update
        { "topic": "state:status", "data": { ... } }       // playback status change
        { "topic": "state:scan-progress", "data": { ... }} // background scan progress
        { "topic": "display:*", "data": { ... } }          // processed display messages
    """
    await websocket.accept()
    client_id = None

    try:
        engine = await session_manager.get_or_create(session_name, live=(mode == "live"))
        client_id = await engine.add_client(websocket)

        # Listen for commands from client
        while True:
            try:
                text = await websocket.receive_text()
                cmd = json.loads(text)
                cmd["_ws"] = websocket
                await engine.handle_command(cmd)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from client {client_id}")

    except WebSocketDisconnect:
        pass
    except ValueError as e:
        try:
            await websocket.send_text(json.dumps({
                "topic": "error",
                "data": {"message": str(e)},
            }))
            await websocket.close()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if client_id is not None:
            engine.remove_client(client_id)
            asyncio.create_task(_delayed_cleanup(session_name))


async def _delayed_cleanup(session_name: str, delay: float = 30.0):
    """Remove session engine if no clients reconnect within delay."""
    await asyncio.sleep(delay)
    await session_manager.cleanup_empty()
