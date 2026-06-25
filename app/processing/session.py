"""
Session Engine — DB-driven playback + WebSocket broadcaster.

One SessionEngine per active session. Multiple WebSocket clients can connect
to the same engine and receive identical output.

The engine reads pre-computed display messages from SQLite (written by
SessionPreProcessor) and broadcasts them to clients at clock rate.
Seeking is instant via DB lookups — no message replay.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket

from app.config import REPLAY_DEBUG, CACHE_DIR
from app.processing.clock import PlaybackClock, ClockState
from app.processing.database import SessionDatabase
from app.processing.file_reader import read_jsonl, load_subscribe_json, _parse_timestamp
from app.processing.preprocessor import SessionPreProcessor
from app.services.live_capture import live_capture

logger = logging.getLogger(__name__)

TICK_INTERVAL = 0.016  # ~60fps tick rate

# Live-edge audio/data sync (card 78). The live playback edge is capped at
# min(data_edge, audio_edge) − this buffer, pinning playback to whichever
# stream is lagging so neither feed is outrun and audio/data stay aligned at
# the edge. The buffer is wiggle room for buffering / download delay.
LIVE_EDGE_BUFFER_S = 8.0

# Keep the transient scratch DB after the last client disconnects (for
# inspection) instead of deleting it.
_DEBUG = REPLAY_DEBUG


def _read_last_line(path: Path) -> Optional[str]:
    """Last non-empty line of a file, read from the end — cheap for an
    append-only log polled every tick."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "ignore")
    except OSError:
        return None
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    return lines[-1] if lines else None


class SessionEngine:
    """Orchestrates replay/live playback for a single session via DB."""

    def __init__(self, session_path: Path, session_name: str, session_type: str = "",
                 live: bool = False):
        self._session_path = session_path
        self._session_name = session_name
        self._session_type = session_type
        self._live = live

        # Core components
        self._clock: Optional[PlaybackClock] = None
        self._db: Optional[SessionDatabase] = None
        self._preprocessor: Optional[SessionPreProcessor] = None

        # Session metadata
        self._initial_state: dict[str, Any] = {}
        self._session_info: dict[str, Any] = {}
        self._gmt_offset: Optional[str] = None
        self._audio_info: Optional[dict[str, Any]] = None
        self._start_time: Optional[datetime] = None
        self._end_time: Optional[datetime] = None
        self._duration: float = 0.0
        # Full session length from the time-bounds scan. During a replay build
        # _duration follows the growing build edge (like a live edge); this
        # holds the final length to pin once the build completes.
        self._scanned_duration: float = 0.0

        # Playback state
        self._last_offset_ms = 0
        self._preprocess_done = asyncio.Event()
        # Set once the offset-0 baseline (driverList, trackGeometry,
        # trackCircuit, sessionInfo) is committed to the DB. add_client waits
        # on this before the connect restore so the build can't be beaten to
        # the punch, leaving tiles blank until a manual seek (card 77).
        self._baseline_ready = asyncio.Event()

        # WebSocket clients
        self._clients: dict[int, WebSocket] = {}
        self._client_counter = 0

        # Lifecycle
        self._preprocess_task: Optional[asyncio.Task] = None
        self._playback_task: Optional[asyncio.Task] = None
        self._raw_stream_task: Optional[asyncio.Task] = None
        self._duration_task: Optional[asyncio.Task] = None
        self._running = False
        # True once the live-mode initial "seek to live edge" has happened.
        # Subsequent clients (reloads, additional tabs) inherit the current
        # playback position rather than yanking the clock forward.
        self._initial_live_seek_done = False

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def clock(self) -> Optional[PlaybackClock]:
        return self._clock

    async def start(self) -> None:
        """Initialize and start the session engine."""
        logger.info(f"Starting session engine for {self._session_name}")

        # Load initial state
        self._initial_state = load_subscribe_json(self._session_path)
        self._session_info = self._initial_state.get("SessionInfo", {})
        self._gmt_offset = self._session_info.get("GmtOffset")

        # Load audio info — prefer the EARLIEST segment for multi-segment
        # captures. The audio endpoint streams segments oldest-first, so
        # the start_utc that aligns with the start of the playback is the
        # one belonging to the lowest-numbered segment (audio_info.001.json),
        # not the latest (audio_info.json).
        audio_info_file = self._session_path / "audio_info.json"
        rotated_info = sorted(self._session_path.glob("audio_info.[0-9][0-9][0-9].json"))
        if rotated_info:
            audio_info_file = rotated_info[0]
        if audio_info_file.exists():
            with open(audio_info_file, "r", encoding="utf-8") as f:
                self._audio_info = json.load(f)
            # Per-segment map (start_utc + duration), chronological, so a
            # multi-segment REPLAY can map the data clock to the audio stream
            # piecewise and SKIP the real-time gap between capture segments
            # (issue I15). Single-segment sessions get a 1-entry list (no-op).
            # Run in a worker thread — it shells out to ffprobe per segment,
            # which would otherwise block the event loop (and every other
            # client/session on it) during engine startup.
            self._audio_info["segments"] = await asyncio.to_thread(
                self._build_audio_segments)

        # Detect session type from SessionInfo if not provided
        if not self._session_type:
            si_type = (self._session_info.get("Type") or "").lower()
            si_name = (self._session_info.get("Name") or "").lower()
            if "qualifying" in si_type or "qualifying" in si_name or "shootout" in si_type:
                self._session_type = "qualifying"
            elif si_type in ("race", "sprint"):
                self._session_type = "race"
            else:
                self._session_type = "practice"

        # The processed DB is a TRANSIENT scratch file (./tmp). live.jsonl is
        # the only permanent source.
        #  - live capture in progress → its preprocessor is populating the
        #    scratch DB as data arrives; the engine just reads it.
        #  - replay → always delete any prior scratch DB and rebuild fresh, so
        #    it's reprocessed by the latest code each time.
        self._db = SessionDatabase(self._session_path)
        capturing = live_capture.is_capturing_path(self._session_path)
        if not capturing:
            base = self._db._db_path
            for suffix in ("", "-wal", "-shm"):
                base.with_name(base.name + suffix).unlink(missing_ok=True)
        self._db.open()

        # Determine session time bounds from JSONL file
        await self._scan_time_bounds()

        self._running = True

        if capturing:
            self._preprocess_done.set()
            # Live capture has already populated the DB — baseline is present.
            self._baseline_ready.set()
            logger.info(f"Session DB built live by capture: {self._session_name}")
        else:
            logger.info(f"Building transient DB for {self._session_name}")
            self._preprocessor = SessionPreProcessor(
                self._session_path, self._session_type,
            )
            self._preprocess_task = asyncio.create_task(
                self._run_preprocess()
            )
            # Stream-immediately: clients connect and play while the transient
            # DB is still building — exactly like a live session, where the
            # edge is the capture head. Here the edge is the build progress.
            # _duration follows that growing edge until the build finishes,
            # then _run_preprocess pins it to the full scanned length.
            self._scanned_duration = self._duration
            self._duration = 0.0

        # Track the moving edge — the live capture head OR (for replay) the
        # build progress — independently of playback so the scrubber stays
        # current even while the clock is paused or rewound.
        if self._live or not self._preprocess_done.is_set():
            self._duration_task = asyncio.create_task(self._track_duration())

    async def _scan_time_bounds(self) -> None:
        """Quick scan to find first and last timestamps from JSONL.

        Only reads first and last lines for speed (avoids full file scan).
        """
        import json as _json
        from app.processing.file_reader import _parse_timestamp

        live_file = self._session_path / "live.jsonl"
        if not live_file.exists():
            return

        first_ts = None
        last_ts = None

        with open(live_file, "r", encoding="utf-8") as f:
            # Read first valid timestamp
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = _json.loads(line)
                    ts = _parse_timestamp(msg.get("DateTime", ""))
                    if ts:
                        first_ts = ts
                        break
                except _json.JSONDecodeError:
                    continue

            # Read last valid timestamp (seek from end)
            # Read all remaining to find the last one
            last_line = line
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped

            if last_line:
                try:
                    msg = _json.loads(last_line)
                    ts = _parse_timestamp(msg.get("DateTime", ""))
                    if ts:
                        last_ts = ts
                except _json.JSONDecodeError:
                    pass

        if first_ts:
            self._start_time = first_ts
            self._clock = PlaybackClock(first_ts)
            if last_ts:
                self._end_time = last_ts
                # Full recording length — playback runs the WHOLE session
                # (post-chequered interviews, podium, cool-down). The scrubber
                # compresses the post-chequered+5min tail into its narrow right
                # region; it does NOT truncate playback.
                self._duration = (last_ts - first_ts).total_seconds()

    async def _run_preprocess(self) -> None:
        """Build the session DB — one-shot fallback used only when no
        complete DB exists and nothing else is building it."""
        try:
            await self._preprocessor.run(
                tail_follow=False,
                on_progress=self._on_preprocess_progress,
                on_baseline_ready=self._baseline_ready.set,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(f"Pre-processing failed for {self._session_name}")
        finally:
            self._preprocessor.close()
            # Build finished: the scrubber now spans the whole session. Pin
            # _duration to the full scanned length (re-applying the post-
            # chequered cap now that the chequered row exists in the DB).
            if self._scanned_duration:
                self._duration = self._scanned_duration
            self._preprocess_done.set()
            # Safety net: if the build ended without ever signalling baseline
            # (e.g. gate timeout, no matching SessionInfo), unblock any waiting
            # connect so it isn't held until its timeout.
            self._baseline_ready.set()

    def _on_preprocess_progress(self, pct: float) -> None:
        """Callback from pre-processor."""
        asyncio.create_task(self._broadcast({
            "topic": "state:scan-progress",
            "data": {"pct": pct},
        }))

    async def stop(self) -> None:
        """Stop and clean up the session engine."""
        self._running = False

        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass

        if self._duration_task and not self._duration_task.done():
            self._duration_task.cancel()
            try:
                await self._duration_task
            except asyncio.CancelledError:
                pass

        await self._stop_raw_stream()

        if self._preprocess_task and not self._preprocess_task.done():
            if self._preprocessor:
                await self._preprocessor.stop()
            self._preprocess_task.cancel()
            try:
                await self._preprocess_task
            except asyncio.CancelledError:
                pass

        if self._db:
            db_path = self._db._db_path
            self._db.close()
            # Transient scratch DB: remove it once no one's viewing, unless
            # DEBUG (keep for inspection) or a live capture still owns it.
            if not _DEBUG and not live_capture.is_capturing_path(self._session_path):
                for suffix in ("", "-wal", "-shm"):
                    db_path.with_name(db_path.name + suffix).unlink(missing_ok=True)

        for ws in list(self._clients.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

        logger.info(f"Session engine stopped for {self._session_name}")

    # ── WebSocket Client Management ──

    async def add_client(self, ws: WebSocket) -> int:
        """Add a WebSocket client. Returns client ID."""
        self._client_counter += 1
        client_id = self._client_counter
        self._clients[client_id] = ws

        # Stream immediately: do NOT wait for the transient-DB build to finish.
        # The client connects and plays from offset 0 while the preprocessor
        # populates the DB; the playback loop and the edge tracker follow the
        # build edge, exactly like a live capture. state:full / state:restore
        # below carry whatever's built so far and playback fills in the rest.

        # Get all event/playbackEvent messages for scrubber
        events = []
        if self._db:
            rows = self._db._conn.execute(
                "SELECT offset_ms, topic, data FROM messages WHERE topic = 'event' ORDER BY offset_ms"
            ).fetchall()
            events = [{"offset_ms": r[0], "topic": r[1], "data": json.loads(r[2])} for r in rows]

        # Send initial state to new client
        await self._send_to_client(ws, {
            "topic": "state:full",
            "data": {
                "sessionType": self._session_type,
                "isLive": self._live,
                "audioInfo": self._build_audio_info_for_client(),
                "startTime": self._start_time.isoformat() if self._start_time else None,
                "endTime": self._end_time.isoformat() if self._end_time else None,
                "duration": self._duration,
                "isPlaying": self._clock.state == ClockState.PLAYING if self._clock else False,
                "speed": self._clock.speed if self._clock else 1.0,
                "offset": self._clock.offset_seconds if self._clock else 0.0,
                "scanProgress": 100.0 if self._preprocess_done.is_set() else 0.0,
                "events": events,
            },
        })

        # For live mode: jump to the live edge ONLY on the first client
        # of an engine's lifetime. Later joins (page reload, second tab,
        # new viewer) inherit the existing clock position so a paused or
        # rewound playback isn't yanked forward by an unrelated connect.
        if self._live and self._clock and self._db and not self._initial_live_seek_done:
            edge_ms = self._capped_edge_ms()
            if edge_ms:
                live_offset = edge_ms / 1000.0
                self._clock.seek_to_offset(live_offset)
                self._duration = live_offset
                self._last_offset_ms = edge_ms
            self._initial_live_seek_done = True

        # Wait for the offset-0 baseline before the connect restore. A
        # still-building replay writes sequentially, so the baseline lands
        # first; serving get_state_at before it commits leaves tiles blank
        # until a manual seek (card 77). Live / already-built sessions have it
        # set already → returns immediately. Timeout is a safety valve.
        try:
            await asyncio.wait_for(self._baseline_ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(
                f"{self._session_name}: baseline not ready after 30s; "
                f"restoring anyway")

        # Send current display state at clock position (all latest messages per topic)
        if self._clock and self._db:
            offset_ms = int(self._clock.offset_seconds * 1000)
            state = self._db.get_state_at(offset_ms)
            if state:
                restore_messages = []
                for topic, entry in state.items():
                    restore_messages.append({
                        "topic": topic,
                        "data": entry["data"],
                        "offset_ms": entry["offset_ms"],
                    })
                await self._send_to_client(ws, {
                    "topic": "state:restore",
                    "data": restore_messages,
                    "offset_ms": offset_ms,
                })

            await self._send_restore_extras(offset_ms, ws)

        logger.info(f"Client {client_id} connected to {self._session_name} ({len(self._clients)} total)")
        return client_id

    def remove_client(self, client_id: int) -> None:
        """Remove a WebSocket client."""
        self._clients.pop(client_id, None)
        logger.info(f"Client {client_id} disconnected from {self._session_name} ({len(self._clients)} total)")

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # ── Client Commands ──

    async def handle_command(self, cmd: dict[str, Any]) -> None:
        """Handle a command from a WebSocket client."""
        action = cmd.get("cmd", "")

        if action == "play":
            await self._play()
        elif action == "pause":
            await self._pause()
        elif action == "seek":
            offset = cmd.get("offset", 0.0)
            await self._seek(offset)
        elif action == "seek_live":
            # Snap to the live edge for live engines; ignored for replay.
            if not self._live:
                logger.warning("seek_live ignored: engine is not live")
                return
            if not self._db:
                logger.warning("seek_live ignored: no DB")
                return
            edge_ms = self._capped_edge_ms()
            if not edge_ms:
                logger.warning("seek_live: no live edge yet")
                return
            target = edge_ms / 1000.0
            # Bump duration so _seek's clamp doesn't pin us behind the
            # actual live edge (the duration tracker only ticks every 1 s).
            if target > self._duration:
                self._duration = target
            logger.info(f"seek_live → offset {target:.1f}s (was {self._clock.offset_seconds:.1f}s)")
            await self._seek(target)
            if self._clock and self._clock.state != ClockState.PLAYING:
                await self._play()
        elif action == "speed":
            value = cmd.get("value", 1.0)
            await self._set_speed(value)
        elif action == "getLapTelemetry":
            driver = cmd.get("driver", "")
            lap = cmd.get("lap", 0)
            await self._send_lap_telemetry(driver, lap, cmd.get("_ws"))
        elif action == "getLastLapTelemetry":
            driver = cmd.get("driver", "")
            await self._send_last_lap_telemetry(driver, cmd.get("_ws"))
        elif action == "getBestLapTelemetry":
            driver = cmd.get("driver", "")
            await self._send_best_lap_telemetry(driver, cmd.get("_ws"))
        else:
            logger.warning(f"Unknown command: {action}")

    # ── Playback Control ──

    async def _play(self) -> None:
        """Start or resume playback."""
        if not self._clock:
            return
        self._clock.play()
        if not self._playback_task or self._playback_task.done():
            self._playback_task = asyncio.create_task(self._playback_loop())
        await self._start_raw_stream(int(self._clock.offset_seconds * 1000))
        await self._broadcast_status()

    async def _pause(self) -> None:
        """Pause playback."""
        if not self._clock:
            return
        self._clock.pause()
        if self._playback_task and not self._playback_task.done():
            self._playback_task.cancel()
            try:
                await self._playback_task
            except asyncio.CancelledError:
                pass
        await self._stop_raw_stream()
        await self._broadcast_status()

    async def _seek(self, offset_seconds: float) -> None:
        """Seek to a specific offset — instant via DB lookup."""
        if not self._clock or not self._db:
            return

        was_playing = self._clock.state == ClockState.PLAYING
        if was_playing:
            self._clock.pause()
            if self._playback_task and not self._playback_task.done():
                self._playback_task.cancel()
                try:
                    await self._playback_task
                except asyncio.CancelledError:
                    pass
        await self._stop_raw_stream()

        # Clamp offset
        offset_seconds = max(0.0, min(offset_seconds, self._duration))
        offset_ms = int(offset_seconds * 1000)

        # Seek the clock
        self._clock.seek_to_offset(offset_seconds)

        # Query latest display message per topic at target offset
        state = self._db.get_state_at(offset_ms)

        # Send state as array of {topic, data, offset_ms} messages
        restore_messages = []
        for topic, entry in state.items():
            restore_messages.append({
                "topic": topic,
                "data": entry["data"],
                "offset_ms": entry["offset_ms"],
            })

        await self._broadcast({
            "topic": "state:restore",
            "data": restore_messages,
            "offset_ms": offset_ms,
        })

        # Refresh telemetry-availability map + replay RC-message history.
        await self._send_restore_extras(offset_ms)

        self._last_offset_ms = offset_ms

        if was_playing:
            self._clock.play()
            self._playback_task = asyncio.create_task(self._playback_loop())
            await self._start_raw_stream(offset_ms)

        await self._broadcast_status()

        # Broadcast clock position
        await self._broadcast({
            "topic": "state:clock",
            "data": {
                "offset": self._clock.offset_seconds,
                "duration": self._duration,
                "speed": self._clock.speed,
            },
        })

    async def _send_restore_extras(self, offset_ms: int, ws=None) -> None:
        """After a state:restore, send the things latest-per-topic can't cover:
        the telemetry-availability map (which driver/lap pairs have a stored
        trace, + which are empty) and the append-only raceControlMessage
        history up to the offset. Per-client (ws) on connect; broadcast on seek.
        """
        if not self._db:
            return

        async def _send(msg):
            if ws:
                await self._send_to_client(ws, msg)
            else:
                await self._broadcast(msg)

        # Telemetry availability map (which driver/lap pairs have a stored trace).
        try:
            by_driver: dict[str, list[int]] = {}
            for driver, lap, dlen in self._db.list_lap_telemetry(offset_ms):
                by_driver.setdefault(driver, []).append(lap)
            if by_driver:
                await _send({"topic": "telemetryAvailable", "data": by_driver})
        except Exception:
            logger.exception("Failed to send telemetry availability map")

        # Race-control message history — append-only, so replay all up to the
        # offset (latest-per-topic restore would only carry the last one).
        try:
            for m in self._db.get_topic_history("raceControlMessage", offset_ms):
                await _send({"topic": "raceControlMessage", "data": m})
        except Exception:
            logger.exception("Failed to replay race control messages")

        # Team-radio history — append-only list of clips (card 8); replay all up
        # to the offset so the Team Radio / RCM list survives connect/seek. The
        # client suppresses auto-play during restore so this doesn't blast audio.
        try:
            for m in self._db.get_topic_history("teamRadio", offset_ms):
                await _send({"topic": "teamRadio", "data": m})
        except Exception:
            logger.exception("Failed to replay team radio history")

        # driverLaps history — the topic is thin (no accumulating laps map), so
        # the client builds its per-lap time map by accumulating lastLap as the
        # messages arrive. Replay the full per-driver history up to the offset so
        # that map survives connect/seek (latest-per-topic would lose it).
        try:
            for topic, data in self._db.get_topic_prefix_history("driverLaps:", offset_ms):
                await _send({"topic": topic, "data": data})
        except Exception:
            logger.exception("Failed to replay driverLaps history")

        # driverLapClassification history — accumulating per-lap state (the
        # client builds lapCls[lap] = type as messages arrive, for pill
        # colours + the IN/OUT legend). Replay the full per-driver history up
        # to the offset so it survives connect/seek; latest-per-topic restore
        # would keep only the last lap's type and every other pill goes white.
        try:
            for topic, data in self._db.get_topic_prefix_history(
                    "driverLapClassification:", offset_ms):
                await _send({"topic": topic, "data": data})
        except Exception:
            logger.exception("Failed to replay driverLapClassification history")

        # Scrubber events — the full processed list (event topic: GREEN / RED /
        # SC / VSC / CHEQUERED), sent on connect AND seek so the scrubber renders
        # every marker built so far (incl. chequered) regardless of playback
        # position. On a still-building replay the list grows as the build
        # progresses and a later seek picks up the rest. handleSessionEvents
        # replaces (not appends), so re-sending is idempotent. [I6]
        try:
            rows = self._db._conn.execute(
                "SELECT offset_ms, topic, data FROM messages "
                "WHERE topic = 'event' ORDER BY offset_ms"
            ).fetchall()
            events = [{"offset_ms": r[0], "topic": r[1], "data": json.loads(r[2])}
                      for r in rows]
            await _send({"topic": "state:events", "data": events})
        except Exception:
            logger.exception("Failed to send scrubber events")

    async def _send_lap_telemetry(self, driver: str, lap: int, ws=None) -> None:
        """Query lap telemetry from DB and send to requesting client."""
        if not self._db or not driver or not lap:
            return
        data = self._db.get_telemetry(driver, lap)
        if data is None:
            return
        msg = {"topic": f"telemetryLap:{driver}:{lap}", "data": data}
        if ws:
            await self._send_to_client(ws, msg)
        else:
            await self._broadcast(msg)

    async def _send_last_lap_telemetry(self, driver: str, ws=None) -> None:
        """Send the most recent completed lap's telemetry for a driver,
        relative to the current playback offset.

        For practice/qualifying, restrict to PUSH/LONG laps (cool / out /
        in / pit / aborted laps are skipped — they aren't representative).
        Race has no such filter: any completed lap counts.
        """
        if not self._db or not driver:
            return
        playback_ms = int(self._clock.offset_seconds * 1000) if self._clock else 0

        # Completed laps with telemetry up to the playback offset, newest first.
        rows = self._db._conn.execute(
            "SELECT topic FROM messages WHERE topic LIKE ? AND offset_ms <= ? "
            "ORDER BY offset_ms DESC",
            (f"telemetryLap:{driver}:%", playback_ms),
        ).fetchall()
        if not rows:
            return
        laps = [int(t.split(":")[2]) for (t,) in rows]

        if self._session_type == "race":
            lap = laps[0]
        else:
            # P/Q: only PUSH laps are representative. Build the latest per-lap
            # classification up to the offset, pick the newest PUSH lap.
            cls_rows = self._db._conn.execute(
                "SELECT data FROM messages WHERE topic=? AND offset_ms <= ? "
                "ORDER BY offset_ms",
                (f"driverLapClassification:{driver}", playback_ms),
            ).fetchall()
            types: dict[int, str] = {}
            for (d,) in cls_rows:
                p = json.loads(d)
                types[p["lap"]] = p.get("type")
            lap = next((l for l in laps if types.get(l) == "PUSH"), None)
            if lap is None:
                return

        data = self._db.get_telemetry(driver, lap)
        if data is None:
            return
        msg = {"topic": f"telemetryLap:{driver}:{lap}", "data": data}
        if ws:
            await self._send_to_client(ws, msg)
        else:
            await self._broadcast(msg)

    async def _send_best_lap_telemetry(self, driver: str, ws=None) -> None:
        """Send telemetry for a driver's personal-best lap as of the
        current playback offset.

        Uses the latest `fastestLaps` message at or before the playback
        clock to identify the driver's best lap number, then returns the
        telemetry for that lap.
        """
        if not self._db or not driver:
            return
        playback_ms = int(self._clock.offset_seconds * 1000) if self._clock else 0
        # The driver's best lap as of the playback offset comes from the latest
        # driverLaps snapshot (bestLap.lap), not the old fastestLaps topic.
        row = self._db._conn.execute(
            "SELECT data FROM messages WHERE topic=? "
            "AND offset_ms <= ? ORDER BY offset_ms DESC LIMIT 1",
            (f"driverLaps:{driver}", playback_ms),
        ).fetchone()
        if not row:
            return
        best = (json.loads(row[0]).get("bestLap") or {}).get("lap")
        if not best:
            return
        data = self._db.get_telemetry(driver, best)
        if data is None:
            return
        msg = {"topic": f"telemetryLap:{driver}:{best}", "data": data}
        if ws:
            await self._send_to_client(ws, msg)
        else:
            await self._broadcast(msg)

    # Raw topics replayed at playback speed for tiles (like the race
    # telemetry tile) that consume the raw F1 stream directly rather
    # than the lowercase processed equivalents. Everything in here is
    # also in `RAW_F1_TOPICS` (preprocessor.py), so it's NOT in the DB
    # — the only way to get it on replay is to re-read live.jsonl.
    _REPLAY_RAW_TOPICS = frozenset({
        "CarData.z", "Position.z",
        "DriverList", "TimingData", "TimingAppData",
        "TrackStatus", "RaceControlMessages",
    })

    async def _raw_telemetry_stream(self, from_offset_ms: int) -> None:
        """Stream raw F1 topics from jsonl, paced by the playback clock.

        Originally only CarData.z + Position.z (telemetry samples). Now
        also DriverList / TimingData / TimingAppData / TrackStatus /
        RaceControlMessages because the race telemetry tile subscribes
        to those raw topic names directly — without them the tile has
        no drivers, no timing, no track status and renders empty on
        replay. Runs for the lifetime of a play window; cancelled on
        seek and restarted at the new offset.
        """
        if not self._start_time or not self._clock:
            return
        try:
            async for msg in read_jsonl(
                self._session_path,
                fast=True,
                tail_follow=self._live,
            ):
                if not self._running or self._clock.state != ClockState.PLAYING:
                    return
                if msg.topic not in self._REPLAY_RAW_TOPICS:
                    continue
                off_ms = int((msg.timestamp - self._start_time).total_seconds() * 1000)
                if off_ms < from_offset_ms:
                    continue
                # Wait for the playback clock to catch up
                while self._running and self._clock.state == ClockState.PLAYING:
                    cur_ms = int(self._clock.offset_seconds * 1000)
                    if cur_ms >= off_ms:
                        break
                    delta = (off_ms - cur_ms) / 1000.0 / max(self._clock.speed, 1.0)
                    await asyncio.sleep(min(0.1, delta))
                if not self._running or self._clock.state != ClockState.PLAYING:
                    return
                await self._broadcast({"topic": msg.topic, "data": msg.data})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Raw telemetry stream error")

    async def _start_raw_stream(self, from_offset_ms: int) -> None:
        """(Re)start the raw telemetry streamer at the given offset."""
        await self._stop_raw_stream()
        self._raw_stream_task = asyncio.create_task(
            self._raw_telemetry_stream(from_offset_ms)
        )

    async def _stop_raw_stream(self) -> None:
        if self._raw_stream_task and not self._raw_stream_task.done():
            self._raw_stream_task.cancel()
            try:
                await self._raw_stream_task
            except asyncio.CancelledError:
                pass
        self._raw_stream_task = None

    async def _set_speed(self, speed: float) -> None:
        """Change playback speed."""
        if not self._clock:
            return
        self._clock.speed = speed
        await self._broadcast_status()

    # ── Live Edge (data ∩ audio) ──

    def _data_edge_ms(self) -> Optional[int]:
        """Latest data offset in the DB (ms). None if no messages yet."""
        if not self._db:
            return None
        try:
            row = self._db._conn.execute(
                "SELECT MAX(offset_ms) FROM messages"
            ).fetchone()
        except Exception:
            return None
        return row[0] if row and row[0] else None

    def _audio_edge_offset(self) -> Optional[float]:
        """Audio leading edge as a data-clock offset (seconds): the latest
        broadcast PROGRAM-DATE-TIME — continuously re-anchored in
        pdt_map.jsonl by the PdtTracker — minus the session start. None until
        a PDT has been observed (or when the session carries no audio).
        Read fresh every call: the offset drifts as the broadcast advances."""
        if not self._start_time:
            return None
        pdt_file = self._session_path / "pdt_map.jsonl"
        if not pdt_file.exists():
            return None
        last = _read_last_line(pdt_file)
        if not last:
            return None
        try:
            edge_pdt = _parse_timestamp(json.loads(last).get("edge_pdt_utc", ""))
        except (json.JSONDecodeError, AttributeError):
            return None
        if edge_pdt is None:
            return None
        return (edge_pdt - self._start_time).total_seconds()

    def _capped_edge_ms(self) -> Optional[int]:
        """The playback live edge (ms), capped at the lagging stream.

        Live: min(data_edge, audio_edge) − LIVE_EDGE_BUFFER_S, so playback
        never outruns either feed and audio/data stay aligned at the edge.
        Replay, or live with no audio PDT yet, returns the raw data edge
        (nothing to cap against). None if no data yet."""
        data_ms = self._data_edge_ms()
        if data_ms is None:
            return None
        if not self._live:
            return data_ms
        audio_edge_s = self._audio_edge_offset()
        if audio_edge_s is None:
            return data_ms
        capped = min(data_ms, int(audio_edge_s * 1000)) - int(LIVE_EDGE_BUFFER_S * 1000)
        return max(0, capped)

    # ── Duration Tracking (live mode) ──

    async def _track_duration(self) -> None:
        """Keep self._duration aligned with the live edge of the DB.

        Runs alongside _playback_loop for live engines so the scrubber
        and progress reporting stay current even when the clock is
        paused or rewound (the playback loop only updates duration
        while it's running).
        """
        try:
            while self._running:
                await asyncio.sleep(1.0)
                # Replay: once the build is done the edge is fixed (duration is
                # pinned in _run_preprocess) — stop following. Live keeps going.
                if not self._live and self._preprocess_done.is_set():
                    return
                if not self._db:
                    continue
                edge_ms = self._capped_edge_ms()
                if edge_ms:
                    new_dur = edge_ms / 1000.0
                    if new_dur > self._duration:
                        self._duration = new_dur
        except asyncio.CancelledError:
            raise

    # ── Playback Loop ──

    async def _playback_loop(self) -> None:
        """Read pre-computed display messages from DB at clock rate."""
        try:
            while self._running and self._clock and self._clock.state == ClockState.PLAYING:
                self._clock.tick()
                target_offset_ms = int(self._clock.offset_seconds * 1000)

                # Fetch display messages between last_offset and target_offset
                if self._db and target_offset_ms > self._last_offset_ms:
                    new_messages = self._db.get_messages_in_range(
                        self._last_offset_ms, target_offset_ms
                    )

                    # Deduplicate: only send latest per topic in this tick
                    latest: dict[str, tuple[int, Any]] = {}
                    for offset, topic, data in new_messages:
                        latest[topic] = (offset, data)

                    # Broadcast to clients with offset_ms
                    for topic, (offset, data) in latest.items():
                        await self._broadcast({"topic": topic, "data": data, "offset_ms": offset})

                # Send clock update
                await self._broadcast({
                    "topic": "state:clock",
                    "data": {
                        "offset": self._clock.offset_seconds,
                        "duration": self._duration,
                        "speed": self._clock.speed,
                    },
                })

                self._last_offset_ms = target_offset_ms

                # Check for end of session
                duration_ms = int(self._duration * 1000)
                if target_offset_ms >= duration_ms:
                    if self._live or not self._preprocess_done.is_set():
                        # Live capture OR replay-still-building: the edge is
                        # moving. Don't end — poll the capped edge; if it has
                        # grown, extend duration, else wait. For live the cap
                        # holds playback at the lagging stream (audio vs data),
                        # so reaching it waits for the laggard to advance.
                        edge_ms = self._capped_edge_ms()
                        if edge_ms and edge_ms > duration_ms:
                            self._duration = edge_ms / 1000.0
                        else:
                            # Nothing new at the capped edge yet — slow polling
                            await asyncio.sleep(0.5)
                            continue
                    else:
                        self._clock.pause()
                        await self._broadcast_status()
                        break

                await asyncio.sleep(TICK_INTERVAL)

        except asyncio.CancelledError:
            raise

    # ── Broadcasting ──

    async def _broadcast(self, message: dict[str, Any]) -> None:
        """Send a message to all connected WebSocket clients."""
        if not self._clients:
            return

        data = json.dumps(message)
        disconnected = []

        for client_id, ws in list(self._clients.items()):
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.append(client_id)

        for client_id in disconnected:
            self._clients.pop(client_id, None)

    async def _send_to_client(self, ws: WebSocket, message: dict[str, Any]) -> None:
        """Send a message to a specific client."""
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            pass

    async def _broadcast_status(self) -> None:
        """Send current playback status to all clients."""
        await self._broadcast({
            "topic": "state:status",
            "data": {
                "isPlaying": self._clock.state == ClockState.PLAYING if self._clock else False,
                "speed": self._clock.speed if self._clock else 1.0,
                "offset": self._clock.offset_seconds if self._clock else 0.0,
                "duration": self._duration,
            },
        })

    # ── Helpers ──

    def _get_duration(self) -> float:
        return self._duration

    def _build_audio_segments(self) -> list:
        """[{start_utc, duration}] per audio segment, chronological — lets a
        multi-segment replay map the data clock to audio piecewise and skip the
        inter-segment capture gap (I15). start_utc per segment from its own
        audio_info.NNN.json; duration via ffprobe."""
        import subprocess
        rotated = sorted(self._session_path.glob("commentary.[0-9][0-9][0-9].aac"))
        current = self._session_path / "commentary.aac"
        segs = list(rotated) + ([current] if current.exists() else [])
        out: list[dict[str, Any]] = []
        for seg in segs:
            if seg.name == "commentary.aac":
                info = self._session_path / "audio_info.json"
            else:
                info = self._session_path / f"audio_info.{seg.name.split('.')[1]}.json"
            start_utc = None
            if info.exists():
                try:
                    start_utc = json.loads(info.read_text()).get("start_utc")
                except (json.JSONDecodeError, OSError):
                    pass
            try:
                p = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(seg)],
                    capture_output=True, text=True, timeout=10)
                dur = float(p.stdout.strip() or 0)
            except (subprocess.SubprocessError, ValueError, FileNotFoundError):
                dur = 0.0
            out.append({"start_utc": start_utc, "duration": dur})
        return out

    def _build_audio_info_for_client(self) -> Optional[dict[str, Any]]:
        """Return audio metadata to ship to a new client.

        For an active live capture the audio endpoint serves bytes from
        slightly before EOF (so the browser has data immediately). The
        `start_utc` reported here must match the wall-clock time those
        bytes correspond to, otherwise the displayed audio timestamp
        will be off.
        """
        if not self._audio_info:
            return None
        if live_capture.is_capturing_path(self._session_path):
            _, effective_start = live_capture.get_live_stream_position(self._session_path)
            if effective_start:
                return {**self._audio_info, "start_utc": effective_start}
        return self._audio_info


class SessionManager:
    """Manages active session engines. One engine per session."""

    def __init__(self, cache_dir: Optional[str] = None):
        # Default to the OS-appropriate cache location (card 25).
        self._cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._engines: dict[str, SessionEngine] = {}

    async def get_or_create(self, session_name: str, live: bool = False) -> SessionEngine:
        """Get an existing engine or create a new one for the session."""
        if session_name in self._engines:
            engine = self._engines[session_name]
            # Reuse if it has clients, or if it's a live engine (a live
            # engine follows the still-growing DB — keep it alive across
            # reloads rather than re-creating it).
            if engine.client_count > 0 or engine._live:
                return engine
            # Replay engine with no clients — discard and rebuild.
            await engine.stop()
            del self._engines[session_name]

        session_path = self._find_session_path(session_name)
        if not session_path:
            raise ValueError(f"Session not found: {session_name}")

        # Infer session type from directory name
        session_type = self._infer_session_type(session_path)

        engine = SessionEngine(session_path, session_name, session_type, live=live)
        await engine.start()
        self._engines[session_name] = engine
        return engine

    @staticmethod
    def _infer_session_type(session_path: Path) -> str:
        """Infer session type from the directory name.

        Handles session key prefix (e.g., '11235_Race' -> 'race').
        """
        name = session_path.name.lower()
        # Strip leading session key prefix (digits followed by underscore)
        parts = name.split("_", 1)
        if parts[0].isdigit() and len(parts) > 1:
            name = parts[1]
        if "qualifying" in name or "shootout" in name:
            return "qualifying"
        if name in ("race", "sprint"):
            return "race"
        return "practice"

    async def remove(self, session_name: str) -> None:
        """Stop and remove a session engine."""
        engine = self._engines.pop(session_name, None)
        if engine:
            await engine.stop()

    async def cleanup_empty(self) -> None:
        """Remove engines with no connected clients (excluding live ones)."""
        empty = [
            name for name, engine in self._engines.items()
            if engine.client_count == 0 and not engine._live
        ]
        for name in empty:
            await self.remove(name)

    def _find_session_path(self, session_name: str) -> Optional[Path]:
        """Find the actual path of a cached session.

        Handles: year_eventNum_location_session format (e.g., 2025_01_Melbourne_Race)
        """
        parts = session_name.split("_")
        if len(parts) >= 4 and parts[0].isdigit():
            year = parts[0]
            year_dir = self._cache_dir / year
            if year_dir.exists():
                for event_dir in year_dir.iterdir():
                    if not event_dir.is_dir():
                        continue
                    event_parts = event_dir.name.split("_", 1)
                    if len(event_parts) > 1:
                        event_number = event_parts[0]
                        location = event_parts[1]
                        for session_dir in event_dir.iterdir():
                            if not session_dir.is_dir():
                                continue
                            # Match with full session dir name (includes session key prefix)
                            cache_key = f"{year}_{event_number}_{location}_{session_dir.name}"
                            if cache_key == session_name:
                                return session_dir
                            # Also match without session key prefix
                            # e.g. folder "11247_Practice_2" matches URL "Practice_2"
                            dir_parts = session_dir.name.split("_", 1)
                            if dir_parts[0].isdigit() and len(dir_parts) > 1:
                                stripped_key = f"{year}_{event_number}_{location}_{dir_parts[1]}"
                                if stripped_key == session_name:
                                    return session_dir

        # Fallback: try legacy flat path
        legacy_path = self._cache_dir / session_name
        if legacy_path.exists():
            return legacy_path

        return None


# Global instance
session_manager = SessionManager()
