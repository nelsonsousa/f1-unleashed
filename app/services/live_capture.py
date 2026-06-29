"""
Live Capture Service.

Manages live SignalR connections to F1's timing service.
Captures timing data to disk (JSONL) and audio commentary (AAC).
Does NOT handle streaming to clients — that's SessionEngine's job.

Audio lifecycle: starts when the AudioStreams message provides the HLS URL,
stops when the capture ends (session over, cancelled, or error).
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

from app import settings
from app.config import CACHE_DIR
from app.processing.preprocessor import SessionPreProcessor
from app.services.signalr_client import F1SignalRClient

logger = logging.getLogger(__name__)


def _norm_session_type(name: str) -> str:
    """Map an F1 session Name/Type to a settings session-type key (card 27)."""
    n = (name or "").lower()
    if "practice" in n:
        return "practice"
    if "qualifying" in n or "shootout" in n:   # incl. sprint qualifying
        return "qualifying"
    return "race"                              # race + sprint


def kill_orphan_ffmpeg(cache_root: Optional[str] = None) -> None:
    """Kill any leftover ffmpeg processes writing to commentary.aac.

    A previous server crash or hard kill can leave ffmpeg children
    alive (their parent dies, they get reparented to init/launchd).
    On the next start they'd race the new server's ffmpeg, both
    writing to the same `-y` (truncated) file and corrupting the
    audio. Sweep them up at startup.
    """
    needle = f"{cache_root or CACHE_DIR}/"
    try:
        out = subprocess.check_output(
            ["pgrep", "-fl", "ffmpeg.*commentary.aac"], text=True
        )
    except subprocess.CalledProcessError:
        return  # No matches
    except FileNotFoundError:
        logger.warning("pgrep not available; skipping orphan ffmpeg sweep")
        return

    killed = 0
    for line in out.splitlines():
        try:
            pid_str, cmd = line.split(maxsplit=1)
            if needle not in cmd:
                continue
            pid = int(pid_str)
            if pid == os.getpid():
                continue
            os.kill(pid, signal.SIGTERM)
            killed += 1
            logger.info(f"Killed orphan ffmpeg pid={pid}")
        except (ValueError, ProcessLookupError, PermissionError) as e:
            logger.debug(f"Skipping line '{line}': {e}")
    if killed:
        logger.info(f"Orphan ffmpeg sweep killed {killed} process(es)")


class CaptureStatus(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CAPTURING = "capturing"
    COMPLETED = "completed"
    ERROR = "error"


class LiveCaptureService:
    """
    Captures live F1 timing data from SignalR and writes to disk.

    The captured JSONL files can then be streamed to clients via SessionEngine.
    """

    # Stale-audio-download watchdog (card 97): ffmpeg has -reconnect, but a dead
    # or expired HLS URL can leave it hung with the output file not growing. We
    # poll commentary.aac's size and restart ffmpeg when it stops growing.
    _AUDIO_CHECK_S = 10         # poll cadence
    _AUDIO_STALE_S = 90         # no file growth this long → stalled (was 30s; too strict —
                                #   HLS lulls / brief network blips tripped false restarts)
    _AUDIO_WIND_DOWN_S = 600    # don't restart if non-heartbeat data idle this long (session ending)
    # SignalR data-stall watchdog: the connection can go silently half-open (no
    # data, not even Heartbeats) with no error. Heartbeats are the liveness signal;
    # if NONE arrive for _SIGNALR_STALL_S while connected, force a reconnect.
    _SIGNALR_CHECK_S = 15       # poll cadence
    _SIGNALR_STALL_S = 60       # no message (incl. Heartbeat) this long → force reconnect

    def __init__(self, cache_dir: Optional[str] = None):
        # Default to the OS-appropriate cache location (card 25).
        self.cache_dir = Path(cache_dir) if cache_dir else CACHE_DIR
        self._captures: dict[str, dict] = {}  # session_id -> capture info
        self._tasks: dict[str, asyncio.Task] = {}
        self._audio_process: Optional[subprocess.Popen] = None
        # Last HLS commentary URL ffmpeg is recording (for the stale-download
        # restart watchdog). Set in _start_audio, cleared in _stop_audio.
        self._audio_url: Optional[str] = None
        # Team-radio download (card 8): session's CDN static Path + dedup set +
        # captures buffered until the static Path is known + in-flight tasks.
        self._radio_static_path: Optional[str] = None
        self._radio_seen: set[str] = set()
        self._radio_pending: list[dict] = []
        self._radio_tasks: set[asyncio.Task] = set()
        # Normalised session type (practice/qualifying/race), from SessionInfo —
        # gates the per-session-type capture toggles (card 27).
        self._capture_session_type: Optional[str] = None
        # PDT side-car: polls the HLS playlist and rewrites
        # audio_info.json:start_utc with a broadcast-derived anchor.
        # Started in _start_audio, stopped in _stop_audio.
        self._pdt_tracker = None

    def _generate_session_id(self) -> str:
        import uuid
        return str(uuid.uuid4())[:8]

    async def start_live(
        self,
        year: int,
        meeting_name: str,
        session_type: str,
        round_number: int = 0,
        meeting_key: int = 0,
        session_name: str = "",
        session_key: int = 0,
    ) -> str:
        """
        Start capturing live timing data from F1 SignalR.

        Data is written to disk in JSONL format. SessionEngine can read
        this file with tail_follow=True to stream to WebSocket clients.

        Returns:
            Session ID for tracking the capture.
        """
        session_id = self._generate_session_id()

        # Build cache directory path
        event_num = (
            str(meeting_key) if meeting_key > 0
            else (f"{round_number:02d}" if round_number > 0 else "00")
        )
        event_name = meeting_name.replace(" ", "_")

        folder_name = (session_name or session_type).replace(" ", "_")

        # Prefix folder with session key for unique identification
        if session_key:
            folder_name = f"{session_key}_{folder_name}"

        cache_path = self.cache_dir / str(year) / f"{event_num}_{event_name}" / folder_name
        cache_path.mkdir(parents=True, exist_ok=True)

        self._captures[session_id] = {
            "status": CaptureStatus.CONNECTING,
            "cache_path": cache_path,
            "message_count": 0,
            "error": None,
        }

        self._tasks[session_id] = asyncio.create_task(
            self._capture_loop(session_id, cache_path)
        )

        return session_id

    def cache_path_for(self, session_id: str):
        """The on-disk cache directory for an active capture (or None).
        Used to co-locate weather-radar tiles in the same session dir."""
        cap = self._captures.get(session_id)
        return cap.get("cache_path") if cap else None

    async def _capture_loop(self, session_id: str, cache_path: Path) -> None:
        """Connect to SignalR and capture messages to disk.

        Alongside capture, a tail-follow SessionPreProcessor builds
        session.db as data arrives, so the processed DB is ready (and
        marked complete) the moment the capture ends — no viewer needed.
        """
        signalr_client = None
        capture = self._captures[session_id]
        preprocessor: Optional[SessionPreProcessor] = None
        preprocess_task: Optional[asyncio.Task] = None

        try:
            logger.info(f"Starting live capture {session_id} -> {cache_path}")

            # timeout=0 disables the SignalR client's internal idle timeout.
            # Live sessions can have long quiet periods before cars go on track
            # (e.g. 30+ minutes between "session live" and first car out).
            # The live_session_monitor in main.py manages the capture lifecycle
            # by polling SessionInfo.json for ArchiveStatus changes.
            signalr_client = F1SignalRClient(
                cache_path=cache_path,
                timeout=0,
            )

            loop = asyncio.get_event_loop()
            message_queue = signalr_client.start(loop)

            capture["status"] = CaptureStatus.CAPTURING
            logger.info(f"Live capture started: {session_id}")

            # Audio-stop watchdog: end ffmpeg when the session is
            # actually over, not when SignalR finally goes idle hours
            # later. Triggers (any of):
            #   1. audio file silent for the trailing 60 s,
            #   2. _SessionEnd message arrived in jsonl,
            #   3. fallback: 30 min since last non-heartbeat message.
            self._last_non_heartbeat_ts = time.monotonic()
            self._session_end_seen = False
            self._radio_static_path = None
            self._radio_seen = set()
            self._radio_pending = []
            self._capture_session_type = None
            audio_watchdog = asyncio.create_task(
                self._audio_session_end_watchdog(cache_path)
            )
            # Restart ffmpeg if the commentary download stalls mid-session (card 97).
            audio_stale_watchdog = asyncio.create_task(
                self._audio_stale_watchdog(cache_path)
            )
            # Force a SignalR reconnect if the data feed goes silently dead
            # (connected but no messages/Heartbeats) — it won't self-recover.
            signalr_stall_watchdog = asyncio.create_task(
                self._signalr_stall_watchdog(signalr_client)
            )

            while capture["status"] == CaptureStatus.CAPTURING:
                try:
                    message = await asyncio.wait_for(message_queue.get(), timeout=30.0)

                    if message.get("type") == "status":
                        status = message.get("status")
                        if status == "reconnecting":
                            # Transient drop — the client is reconnecting and
                            # will keep appending to live.jsonl. Do NOT end.
                            logger.warning("SignalR reconnecting…")
                        elif status == "disconnected":
                            # Terminal — the client has stopped for good.
                            logger.info("SignalR disconnected (terminal)")
                            break
                    elif message.get("type") == "error":
                        logger.error(f"SignalR error: {message.get('message')}")
                    elif message.get("type") == "timing":
                        capture["message_count"] = signalr_client.message_count

                        topic = message.get("topic")
                        # Track non-heartbeat activity for the 30-min fallback.
                        if topic and topic != "Heartbeat":
                            self._last_non_heartbeat_ts = time.monotonic()
                        # Hard session-end signal from F1.
                        if topic == "_SessionEnd":
                            self._session_end_seen = True

                        # Start the DB preprocessor once live.jsonl exists
                        # (the SignalR client writes it on first data).
                        if preprocessor is None and (cache_path / "live.jsonl").exists():
                            preprocessor = SessionPreProcessor(cache_path, "")
                            preprocess_task = asyncio.create_task(
                                preprocessor.run(tail_follow=True)
                            )
                            logger.info(f"DB preprocessor started for capture {session_id}")

                        # Start audio when AudioStreams provides the URL
                        if topic == "AudioStreams":
                            self._handle_audio_streams(message.get("data"), cache_path)
                        # Team radio (card 8): static Path → download new clips.
                        elif topic == "SessionInfo":
                            self._capture_radio_static_path(message.get("data"), cache_path)
                        elif topic == "TeamRadio":
                            self._handle_team_radio(message.get("data"), cache_path)

                except asyncio.TimeoutError:
                    # Only end if the capture thread has truly died — a brief
                    # is_connected==False happens during a reconnect and must
                    # NOT end the capture (the client owns reconnection).
                    if not signalr_client.is_alive:
                        logger.warning("SignalR capture thread ended")
                        break

            capture["status"] = CaptureStatus.COMPLETED
            logger.info(
                f"Live capture completed: {session_id}, "
                f"{signalr_client.message_count} messages"
            )

        except asyncio.CancelledError:
            logger.info(f"Live capture cancelled: {session_id}")
            raise

        except Exception as e:
            logger.error(f"Live capture error: {e}", exc_info=True)
            capture["status"] = CaptureStatus.ERROR
            capture["error"] = str(e)

        finally:
            # Cancel the audio watchdog before we hard-stop audio in the
            # finally block so it doesn't race the cleanup.
            try:
                audio_watchdog.cancel()
            except NameError:
                pass
            try:
                audio_stale_watchdog.cancel()
            except NameError:
                pass
            try:
                signalr_stall_watchdog.cancel()
            except NameError:
                pass
            self._stop_audio(cache_path)
            # Audio is anchored solely by PdtTracker (byte-0 PROGRAM-DATE-TIME);
            # no end-of-capture re-anchor.
            if signalr_client:
                signalr_client.stop()

            # Finalize the DB build. SignalR is stopped first (above), so
            # all data is on disk; a graceful stop lets the preprocessor
            # drain the last lines and mark session.db complete.
            if preprocessor is not None:
                await preprocessor.stop()
                if preprocess_task is not None:
                    try:
                        await preprocess_task
                    except Exception:
                        logger.exception(
                            f"DB preprocessor error for capture {session_id}"
                        )
                preprocessor.close()
                logger.info(f"DB preprocessor finalized for capture {session_id}")

            # Per-session-type "keep downloaded files" toggle (card 27): when off,
            # drop the session cache (live.jsonl, audio, radio) now the build is
            # finalised — the user watched live and wants no stored replay.
            stype = self._capture_session_type
            if stype and not settings.get(f"keepFiles.{stype}", True):
                import shutil
                try:
                    shutil.rmtree(cache_path)
                    logger.info(f"keepFiles off — removed session cache {cache_path}")
                except OSError as e:
                    logger.warning(f"keepFiles cleanup failed: {e}")

    # ── Audio Recording ──

    def _handle_audio_streams(self, data, cache_path: Path) -> None:
        """Extract HLS URL from AudioStreams message and start recording."""
        if self._audio_process and self._audio_process.poll() is None:
            return  # Already recording
        # Per-session-type commentary toggle (card 27).
        stype = self._capture_session_type
        if stype and not settings.get(f"audio.{stype}", True):
            return

        try:
            streams = data if isinstance(data, list) else (data or {}).get("Streams", [])
            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                uri = stream.get("Uri")
                if uri:
                    # Use the stream's Utc as audio start reference
                    stream_utc = stream.get("Utc")
                    self._start_audio(uri, cache_path, stream_utc)
                    return
        except Exception as e:
            logger.error(f"Failed to parse AudioStreams: {e}")

    # ── Team radio (card 8) ──
    def _capture_radio_static_path(self, data, cache_path: Path) -> None:
        """Record the session's CDN static Path + normalised type from SessionInfo,
        then flush any TeamRadio captures that arrived before the Path was known."""
        if not isinstance(data, dict):
            return
        if self._capture_session_type is None and (data.get("Type") or data.get("Name")):
            self._capture_session_type = _norm_session_type(
                data.get("Type") or data.get("Name"))
        if self._radio_static_path or not data.get("Path"):
            return
        self._radio_static_path = data["Path"]
        if self._radio_pending:
            pending, self._radio_pending = self._radio_pending, []
            self._spawn_radio_download(cache_path, pending)

    def _handle_team_radio(self, data, cache_path: Path) -> None:
        """Download newly-listed team-radio clips. Buffers until the static Path
        (from SessionInfo) is known if TeamRadio arrives first."""
        # Per-session-type team-radio toggle (card 27).
        stype = self._capture_session_type
        if stype and not settings.get(f"teamRadio.{stype}", True):
            return
        from app.services import team_radio
        new = [c for c in team_radio.extract_captures(data)
               if c.get("Path") and c["Path"] not in self._radio_seen]
        if not new:
            return
        for c in new:
            self._radio_seen.add(c["Path"])
        if not self._radio_static_path:
            self._radio_pending.extend(new)
            return
        self._spawn_radio_download(cache_path, new)

    def _spawn_radio_download(self, cache_path: Path, captures: list) -> None:
        from app.services import team_radio
        task = asyncio.create_task(
            team_radio.download_captures(cache_path, self._radio_static_path, captures))
        self._radio_tasks.add(task)
        task.add_done_callback(self._radio_tasks.discard)

    def _start_audio(self, url: str, cache_path: Path, stream_utc: str = None) -> None:
        """Start ffmpeg recording of HLS commentary stream.

        Computes the true audio start time by reading the HLS playlist's
        PROGRAM-DATE-TIME tag. The tag is a running counter since the stream
        was created; subtracting it from wall clock gives when the stream began.
        The first segment in the playlist (which ffmpeg will record from) maps
        to: now - program_date_time_seconds.

        IMPORTANT: this is called every time AudioStreams arrives — on the
        initial subscribe and on every SignalR reconnect during a session.
        We MUST NOT let ffmpeg open the output with O_TRUNC (which is what
        the `-y` flag implies when the file already exists), because doing
        so wipes the audio captured so far in the same session. Instead,
        rotate the existing recording into `commentary.NNN.aac` and start
        a fresh ffmpeg writing into `commentary.aac`. The audio endpoint
        concatenates the segments at serve time.
        """
        self._stop_audio(cache_path)

        output_path = cache_path / "commentary.aac"
        now = datetime.utcnow()

        # Rotate existing audio (from a prior ffmpeg run in this session)
        # so we don't truncate it when ffmpeg opens its output for write.
        self._rotate_audio_segment(cache_path)

        # Audio is always captured LIVE — mark start_utc at the moment
        # ffmpeg begins recording (wall-clock now). The user adjusts the
        # offset manually via the audio-controls input box; we no longer
        # try to be clever with HLS PROGRAM-DATE-TIME or signature
        # detection (both have edge cases that caused worse drift than
        # just trusting the live-write timestamp).
        start_utc = now.replace(microsecond=0).isoformat() + "Z"

        try:
            self._audio_process = subprocess.Popen(
                [
                    "ffmpeg", "-nostdin", "-hide_banner",
                    # verbose so ffmpeg logs `Opening '…segment_N.aac' for reading`
                    # — the PdtTracker reads the FIRST such line to learn the exact
                    # byte-0 segment, then matches it to that segment's PDT in the
                    # playlist (race-free anchoring, card zpn5J5U4).
                    "-loglevel", "verbose",
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "30",
                    # 3 segments behind the edge (ffmpeg default, explicit) for a
                    # smooth start; correctness no longer depends on this value.
                    "-live_start_index", "-3",
                    "-i", url,
                    "-c", "copy",
                    # No -y: ffmpeg would otherwise truncate. We've already
                    # ensured `output_path` doesn't exist via _rotate_audio_segment.
                    str(output_path),
                ],
                stdout=subprocess.DEVNULL,
                # Per-run ffmpeg log (fresh each run); PdtTracker tails it for the
                # first opened segment. Child keeps its own dup of the fd.
                stderr=open(cache_path / "audio_ffmpeg.log", "wb"),
            )
            self._audio_url = url   # for the stale-download restart watchdog
            logger.info(f"Started audio recording: {url} -> {output_path}")

            # audio_info.json holds the metadata for the *current* (latest)
            # commentary.aac segment. Per-segment metadata is written
            # alongside in _rotate_audio_segment so the endpoint can
            # reconstruct timestamps for older segments too.
            info_file = cache_path / "audio_info.json"
            with open(info_file, "w", encoding="utf-8") as f:
                json.dump({
                    "url": url,
                    "start_utc": start_utc,
                    "file": "commentary.aac",
                }, f, indent=2)

            # PDT side-car: continuously re-anchors start_utc to the
            # actual broadcast UTC derived from the HLS playlist's
            # PROGRAM-DATE-TIME tag. Replaces the manual sync workflow.
            try:
                from app.services.audio_pdt_tracker import PdtTracker
                self._pdt_tracker = PdtTracker(url, cache_path)
                self._pdt_tracker.start()
            except Exception as e:
                logger.warning(f"PdtTracker did not start: {e}")
                self._pdt_tracker = None

        except FileNotFoundError:
            logger.warning("ffmpeg not found — audio recording disabled")
            self._audio_process = None
        except Exception as e:
            logger.error(f"Failed to start audio recording: {e}")
            self._audio_process = None

    @staticmethod
    def _rotate_audio_segment(cache_path: Path) -> None:
        """Rename existing commentary.aac → commentary.NNN.aac (next free).

        Also rotates audio_info.json → audio_info.NNN.json so each segment
        keeps its own start_utc. Called before starting a fresh ffmpeg so
        the new run doesn't truncate the prior segment.
        """
        current = cache_path / "commentary.aac"
        if not current.exists() or current.stat().st_size == 0:
            return
        # Find next free segment index (3 digits, zero-padded)
        idx = 1
        while True:
            candidate = cache_path / f"commentary.{idx:03d}.aac"
            if not candidate.exists():
                break
            idx += 1
        try:
            current.rename(candidate)
            logger.info(f"Rotated existing audio → {candidate.name}")
        except Exception as e:
            logger.error(f"Failed to rotate audio segment: {e}")
            return
        # Move the matching audio_info.json so per-segment start times
        # survive across rotations.
        info = cache_path / "audio_info.json"
        if info.exists():
            try:
                info.rename(cache_path / f"audio_info.{idx:03d}.json")
            except Exception as e:
                logger.warning(f"Failed to rotate audio_info: {e}")
        # Keep each file's PDT audit map alongside it (audit only).
        ledger = cache_path / "pdt_ledger.json"
        if ledger.exists():
            try:
                ledger.rename(cache_path / f"pdt_ledger.{idx:03d}.json")
            except Exception as e:
                logger.warning(f"Failed to rotate pdt_ledger: {e}")

    @staticmethod
    def _compute_hls_start(master_url: str, now: datetime) -> Optional[str]:
        """Compute the true wall-clock start time of HLS audio.

        Fetches the HLS playlist, finds #EXT-X-PROGRAM-DATE-TIME (a running
        counter since stream creation), and maps the first available segment
        to wall-clock time: start = now - program_date_time_seconds.
        """
        import requests as _req
        try:
            # Fetch master playlist to find the audio sub-playlist URL
            base_url = master_url.rsplit('/', 1)[0] + '/'
            resp = _req.get(master_url, timeout=5)
            if resp.status_code != 200:
                return None

            # Find highest bitrate audio playlist
            sub_url = None
            for line in resp.text.splitlines():
                line = line.strip()
                if line.endswith('.m3u8') and not line.startswith('#'):
                    sub_url = line

            if not sub_url:
                return None

            # Fetch sub-playlist
            if not sub_url.startswith('http'):
                sub_url = base_url + sub_url
            resp2 = _req.get(sub_url, timeout=5)
            if resp2.status_code != 200:
                return None

            # Find #EXT-X-PROGRAM-DATE-TIME
            for line in resp2.text.splitlines():
                if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
                    ts_str = line.split(':', 1)[1].strip()
                    pdt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    pdt_naive = pdt.replace(tzinfo=None)

                    # Detect format: if PDT is close to now (within 1 hour),
                    # it's real UTC. Otherwise it's a running counter from epoch.
                    diff = abs((now - pdt_naive).total_seconds())
                    if diff < 3600:
                        # Real UTC — the first segment in the playlist IS at this time
                        audio_start = pdt_naive
                        logger.info(f"HLS audio start (real UTC): {audio_start.isoformat()}Z")
                    else:
                        # Running counter — subtract from now
                        pdt_seconds = (pdt_naive - datetime(1970, 1, 1)).total_seconds()
                        audio_start = now - timedelta(seconds=pdt_seconds)
                        logger.info(f"HLS audio start (counter): {audio_start.isoformat()}Z "
                                    f"(PDT={pdt_seconds:.0f}s)")
                    return audio_start.isoformat() + 'Z'

            return None
        except Exception as e:
            logger.warning(f"Failed to compute HLS start time: {e}")
            return None

    async def _audio_session_end_watchdog(self, cache_path: Path) -> None:
        """Stop audio capture when the SESSION ends — not when SignalR
        finally goes idle hours later. Polls every 30 s. Triggers on:
          1. `_SessionEnd` topic seen in the SignalR stream,
          2. audio file silent for the trailing 60 s,
          3. fallback: 30 min since the last non-heartbeat message.

        After stopping, trim trailing silence from every commentary
        segment so the captured file isn't padded with dead air.
        """
        # Brief startup grace so ffmpeg has a chance to actually start
        # producing samples before we test for silence.
        await asyncio.sleep(120)

        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return

            if not self._audio_process or self._audio_process.poll() is not None:
                return  # Audio already stopped — nothing left to do.

            reason = None
            if self._session_end_seen:
                reason = "_SessionEnd"
            elif (time.monotonic() - self._last_non_heartbeat_ts) > 1800:
                reason = "30-min-idle fallback"
            elif self._audio_tail_is_silent(cache_path):
                reason = "60 s trailing silence"

            if reason:
                logger.info(
                    f"Stopping audio capture for {cache_path.name}: {reason}"
                )
                self._stop_audio(cache_path)
                self._trim_trailing_silence(cache_path)
                return

    async def _audio_stale_watchdog(self, cache_path: Path) -> None:
        """Restart ffmpeg when the commentary DOWNLOAD stalls (card 97).

        ffmpeg's own -reconnect handles transient segment-fetch drops, but a dead
        or expired HLS URL can leave it hung with commentary.aac not growing (no
        process exit). We poll the file size; if it stops growing for
        _AUDIO_STALE_S while the session is still live, re-run ffmpeg on the same
        URL (which re-resolves the playlist to the current live edge). This is
        distinct from the end-watchdog, which stops audio on *silence* at session
        end (the file still GROWS during silence — ffmpeg writes silent frames)."""
        output = cache_path / "commentary.aac"
        # Startup grace so ffmpeg has a chance to produce its first segment.
        try:
            await asyncio.sleep(self._AUDIO_STALE_S)
        except asyncio.CancelledError:
            return
        last_size, last_grow = -1, time.monotonic()
        while True:
            try:
                await asyncio.sleep(self._AUDIO_CHECK_S)
            except asyncio.CancelledError:
                return
            # Audio intentionally stopped (end-watchdog / shutdown) → reset, idle.
            if not self._audio_url or not self._audio_process:
                last_size, last_grow = -1, time.monotonic()
                continue
            try:
                size = output.stat().st_size if output.exists() else 0
            except OSError:
                size = 0
            now = time.monotonic()
            if size > last_size:
                last_size, last_grow = size, now
                continue
            if (now - last_grow) < self._AUDIO_STALE_S:
                continue
            # Not growing. Don't restart if the whole session is winding down
            # (non-heartbeat data long idle) — the end-watchdog will close it.
            if (now - getattr(self, "_last_non_heartbeat_ts", now)) > self._AUDIO_WIND_DOWN_S:
                continue
            url = self._audio_url
            logger.warning(
                f"Audio download stalled for {cache_path.name} "
                f"({now - last_grow:.0f}s no growth) — restarting ffmpeg")
            try:
                self._start_audio(url, cache_path)
            except Exception as e:
                logger.error(f"Audio restart failed: {e}")
            last_size, last_grow = -1, time.monotonic()

    async def _signalr_stall_watchdog(self, signalr_client) -> None:
        """Force a reconnect when the SignalR data feed goes silently dead.

        The connection can stay 'connected' yet deliver nothing — no timing data
        AND no Heartbeats — with no error or close (a half-open socket). With the
        client's idle timeout disabled (timeout=0), nothing detects this, so the
        capture freezes indefinitely. Heartbeats are server-pushed regardless of
        on-track activity, so a long message-age means a DEAD connection, not a
        quiet session. We act only while CONNECTED; mid-reconnect we let the
        client's own backoff loop work. force_reconnect() drops the socket so the
        reconnect loop re-negotiates + re-subscribes — it never ends a live
        session (per the retry-with-backoff policy)."""
        try:
            await asyncio.sleep(self._SIGNALR_STALL_S)   # startup grace (connect + subscribe)
        except asyncio.CancelledError:
            return
        while True:
            try:
                await asyncio.sleep(self._SIGNALR_CHECK_S)
            except asyncio.CancelledError:
                return
            if signalr_client is None or not signalr_client.is_alive:
                continue                       # thread ended → the capture loop owns it
            if not signalr_client.is_connected:
                continue                       # already reconnecting → let backoff work
            age = signalr_client.last_message_age
            if age > self._SIGNALR_STALL_S:
                logger.warning(
                    f"SignalR data stalled ({age:.0f}s, no messages/Heartbeats) "
                    f"— forcing reconnect")
                signalr_client.force_reconnect()

    def _audio_tail_is_silent(self, cache_path: Path) -> bool:
        """True iff the LAST 60 s of commentary.aac contain no
        non-silent frames (silencedetect -50 dB / 60 s threshold)."""
        af = cache_path / "commentary.aac"
        if not af.exists() or af.stat().st_size < 1024:
            return False
        try:
            # Probe just the tail to keep this cheap. Take last 90 s.
            res = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-v", "error",
                    "-sseof", "-90", "-i", str(af),
                    "-af", "silencedetect=noise=-50dB:d=60",
                    "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=30,
            )
            # silencedetect emits "silence_start: 0" (or similar) when
            # the tail is one long silence; absent → audio present.
            return "silence_start" in (res.stderr or "")
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False  # Don't false-trigger on probe failure.

    def _trim_trailing_silence(self, cache_path: Path) -> None:
        """For every commentary*.aac in the session dir, find the last
        non-silent moment and re-mux the file ending 60 s after it
        (so we keep a 1-min trailing pad). No-op when trailing silence
        is shorter than the 60 s pad."""
        for af in sorted(cache_path.glob("commentary*.aac")):
            try:
                # Get file duration.
                dur_out = subprocess.check_output(
                    ["ffprobe", "-v", "error",
                     "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(af)],
                    text=True, timeout=30,
                ).strip()
                duration = float(dur_out)
            except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
                logger.warning(f"trim: could not probe duration for {af.name}")
                continue

            try:
                # Find every silence_end timestamp (= moment audio
                # resumes). The LAST one is the start of the final
                # audible region; trailing silence runs from there +
                # (segment length captured by silencedetect).
                res = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "info",
                     "-i", str(af),
                     "-af", "silencedetect=noise=-50dB:d=2",
                     "-f", "null", "-"],
                    capture_output=True, text=True, timeout=300,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue

            # Parse "silence_start: NNN.NNN" lines from stderr.
            last_silence_start = None
            for line in (res.stderr or "").splitlines():
                if "silence_start:" in line:
                    try:
                        last_silence_start = float(
                            line.split("silence_start:")[1].split()[0]
                        )
                    except (ValueError, IndexError):
                        pass

            if last_silence_start is None:
                continue  # No silence detected — nothing to trim.

            # If the trailing silence runs to the end of the file AND
            # is longer than 60 s, trim to silence_start + 60 s.
            trailing = duration - last_silence_start
            if trailing <= 60:
                continue  # Less than 60 s — leave as is.

            target = last_silence_start + 60
            tmp = af.with_suffix(".aac.trim")
            try:
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y",
                     "-i", str(af),
                     "-t", f"{target:.3f}",
                     "-c", "copy", str(tmp)],
                    check=True, timeout=300,
                )
                tmp.replace(af)
                logger.info(
                    f"trim: {af.name} {duration:.1f}s -> {target:.1f}s "
                    f"(removed {trailing - 60:.1f}s of trailing silence)"
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                    FileNotFoundError, OSError) as e:
                logger.warning(f"trim: ffmpeg trim of {af.name} failed: {e}")
                if tmp.exists():
                    tmp.unlink()

    def _stop_audio(self, cache_path: Path) -> None:
        """Stop ffmpeg and log file size.

        SIGINT first (lets ffmpeg flush its current segment cleanly), then
        SIGKILL after 5 s if it's still alive, with a final 5 s wait so
        we don't leave a zombie if SIGKILL is also somehow pending.
        """
        # Stop the PDT side-car first so it doesn't keep ffprobing the
        # file while ffmpeg is in the middle of flushing.
        if self._pdt_tracker is not None:
            try:
                self._pdt_tracker.stop()
                self._pdt_tracker.join(timeout=3)
            except Exception as e:
                logger.warning(f"PdtTracker stop failed: {e}")
            self._pdt_tracker = None

        self._audio_url = None   # intentional stop → stale watchdog must not restart

        if not self._audio_process:
            return

        if self._audio_process.poll() is None:
            try:
                self._audio_process.send_signal(signal.SIGINT)
                self._audio_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg did not exit on SIGINT, sending SIGKILL")
                try:
                    self._audio_process.kill()
                    self._audio_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("ffmpeg did not die on SIGKILL — leaking process")
                except Exception as e:
                    logger.warning(f"Error force-killing ffmpeg: {e}")
            except Exception as e:
                logger.warning(f"Error stopping audio recording: {e}")

        self._audio_process = None

        audio_file = cache_path / "commentary.aac"
        if audio_file.exists():
            size_mb = audio_file.stat().st_size / (1024 * 1024)
            logger.info(f"Audio recording saved: {audio_file} ({size_mb:.1f} MB)")

    def get_status(self, session_id: str) -> dict:
        """Get capture status."""
        capture = self._captures.get(session_id)
        if not capture:
            raise ValueError(f"Capture not found: {session_id}")

        return {
            "session_id": session_id,
            "status": capture["status"].value,
            "message_count": capture["message_count"],
            "error": capture["error"],
        }


    def is_capturing_path(self, cache_path: Path) -> bool:
        """Check if a live capture is currently writing to this directory."""
        target = Path(cache_path).resolve()
        for capture in self._captures.values():
            if capture["status"] not in (CaptureStatus.CONNECTING, CaptureStatus.CAPTURING):
                continue
            if Path(capture["cache_path"]).resolve() == target:
                return True
        return False

    @staticmethod
    def get_live_stream_position(cache_path: Path, rewind_bytes: int = 64 * 1024) -> tuple[int, Optional[str]]:
        """Compute (seek_byte, effective_start_utc_iso) for live audio streaming.

        ffmpeg writes the AAC file in HLS-segment-sized bursts (often 15-30 s
        of audio at a time, then idle). If the server seeks straight to EOF,
        the browser can wait that long with zero bytes and abandon playback.
        Rewinding by `rewind_bytes` from EOF gives the browser ~8 s of audio
        immediately, while keeping the stream essentially live.

        The returned start_utc maps audio.currentTime=0 to the wall-clock
        time the file's bytes correspond to at `seek_byte`, computed from
        the actual byte rate of the file (handles bitrate variation).
        """
        audio_file = cache_path / "commentary.aac"
        info_file = cache_path / "audio_info.json"
        if not audio_file.exists():
            return 0, None

        file_size = audio_file.stat().st_size
        if file_size == 0:
            return 0, None

        file_start_utc: Optional[datetime] = None
        if info_file.exists():
            try:
                info = json.loads(info_file.read_text())
                raw = info.get("start_utc")
                if raw:
                    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    file_start_utc = parsed.replace(tzinfo=None)
            except Exception:
                pass

        now = datetime.utcnow()
        if file_start_utc is None:
            return max(0, file_size - rewind_bytes), now.isoformat() + "Z"

        elapsed = (now - file_start_utc).total_seconds()
        if elapsed <= 0:
            return 0, file_start_utc.isoformat() + "Z"

        byterate = file_size / elapsed
        seek_byte = max(0, file_size - rewind_bytes)
        if byterate > 0:
            seek_offset_sec = seek_byte / byterate
            effective_start = file_start_utc + timedelta(seconds=seek_offset_sec)
        else:
            effective_start = file_start_utc
        return seek_byte, effective_start.isoformat() + "Z"

    async def stop(self, session_id: str) -> None:
        """Stop a live capture."""
        capture = self._captures.get(session_id)
        if not capture:
            return

        if session_id in self._tasks:
            self._tasks[session_id].cancel()
            try:
                await self._tasks[session_id]
            except asyncio.CancelledError:
                pass
            del self._tasks[session_id]

        del self._captures[session_id]
        logger.info(f"Capture {session_id} stopped")


# Global instance
live_capture = LiveCaptureService()
