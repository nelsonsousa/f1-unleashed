"""
Session Pre-Processor — transforms raw JSONL into pre-computed display data.

Message gating:
  - All messages are buffered until SessionInfo with the expected session Key arrives.
  - That SessionInfo is emitted first. Buffered messages with the same timestamp are
    kept, rest discarded. Buffer timeout: 60 seconds (discard if no matching SessionInfo).
  - After gating, messages with envelope timestamps >1h before the reference time
    (SessionInfo arrival) are filtered out.

Payload timestamp filtering:
  - For topics like RaceControlMessages, SessionData, etc., individual entries with
    payload timestamps (e.g. Messages.*.Utc) before the cutoff are stripped.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import json as _json

from app.config import DATA_DIR
from app.processing.database import SessionDatabase
from app.processing.file_reader import read_jsonl, load_subscribe_json, RawMessage
from app.processing.message_bus import SessionMessageBus
from app.processing.processors.base import Processor
from app.processing.processors.championship_processor import ChampionshipProcessor
from app.processing.processors.lap_classification_processor import LapClassificationProcessor
from app.processing.processors.lap_prediction_processor import LapPredictionProcessor
from app.processing.processors.clock_processor import ClockProcessor
from app.processing.processors.driver_list_processor import DriverListProcessor
from app.processing.processors.driver_status_processor import DriverStatusProcessor
from app.processing.processors.position_processor import PositionProcessor
from app.processing.processors.race_control_processor import RaceControlProcessor
from app.processing.processors.fia_stewards_processor import FiaStewardsProcessor
from app.processing.processors.session_info_processor import SessionInfoProcessor
from app.processing.processors.standings_processor import StandingsProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor
from app.processing.processors.lap_timing_processor import LapTimingProcessor
from app.processing.processors.lap_delta_processor import LapDeltaProcessor
from app.processing.processors.driver_gap_processor import DriverGapProcessor
from app.processing.processors.race_pace_processor import RacePaceProcessor
from app.processing.processors.team_radio_processor import TeamRadioProcessor
from app.processing.processors.data_health_processor import DataHealthProcessor
from app.processing.processors.heartbeat_processor import HeartbeatProcessor
from app.processing.processors.sector_timing_processor import SectorTimingProcessor
from app.processing.processors.tyre_processor import TyreProcessor
from app.processing.processors.track_status_processor import TrackStatusProcessor
from app.processing.processors.weather_processor import WeatherProcessor

logger = logging.getLogger(__name__)

BUFFER_FLUSH_MESSAGES = 100
BUFFER_FLUSH_MS = 1000
# Yield to the event loop often enough that a client streaming WHILE the
# transient DB is still building (stream-immediately replay) gets smooth
# playback — the build coroutine would otherwise hog the loop for ~3s per
# 2000-message batch, leaving playback updating in chunky jumps. ~50 keeps
# server-side updates around 10-15/s (the client's 2s buffer interpolates the
# rest) at negligible build-time cost; an offline build with no client just
# does a few extra no-op yields.
YIELD_EVERY = 50
GATE_TIMEOUT_S = 60  # Discard buffered messages if no matching SessionInfo within 60s

# Raw F1 topics — not captured to DB (these are input, not output)
RAW_F1_TOPICS = {
    "CarData.z", "Position.z", "DriverList", "TimingData", "TimingDataF1",
    "TimingAppData", "SessionData", "SessionInfo", "SessionStatus",
    "ExtrapolatedClock", "TrackStatus", "WeatherData", "WeatherDataSeries",
    "RaceControlMessages", "LapCount", "ChampionshipPrediction",
    "AudioStreams", "ContentStreams", "Heartbeat", "TeamRadio",
    "TyreStintSeries", "LapSeries", "TopThree", "TimingStats",
    "PitLaneTimeCollection", "PitStopSeries", "PitStop", "CurrentTyres",
    "DriverRaceInfo", "OvertakeSeries", "DriverScore", "SPFeed",
    "ArchiveStatus", "TlaRcm", "RcmSeries", "DriverTracker",
}


# Topic-discovery baseline: every topic name ever seen. Genuinely-new topics
# (absent here) raise an alert; first run (file absent) seeds silently.
KNOWN_TOPICS_FILE = DATA_DIR / "known_topics.json"


def _load_known_topics() -> tuple[set, bool]:
    """Return (known topic set, is_first_run). First run = baseline absent.

    The baseline always includes RAW_F1_TOPICS (the topics we already know
    about) so those never alert; the persisted file accumulates anything
    discovered beyond that.
    """
    base = set(RAW_F1_TOPICS)
    try:
        data = _json.loads(KNOWN_TOPICS_FILE.read_text())
        if isinstance(data, dict):          # catalog format (card 120)
            return base | set(data.keys()), False
        if isinstance(data, list):          # legacy flat-list format
            return base | set(data), False
        return base, True
    except (FileNotFoundError, ValueError, OSError):
        return base, True


def _parse_lap_time_ms(s) -> Optional[int]:
    """Parse F1 lap-time string 'M:SS.mmm' into milliseconds."""
    if not isinstance(s, str):
        return None
    parts = s.split(":")
    if len(parts) != 2:
        return None
    try:
        mins = int(parts[0])
        sp = parts[1].split(".")
        secs = int(sp[0])
        ms = int(sp[1].ljust(3, "0")[:3]) if len(sp) > 1 else 0
        return mins * 60000 + secs * 1000 + ms
    except (ValueError, IndexError):
        return None


def _parse_utc(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _extract_session_key(folder_name: str) -> Optional[int]:
    """Extract session key from folder name like '11236_Sprint_Qualifying'."""
    parts = folder_name.split("_", 1)
    if parts[0].isdigit():
        return int(parts[0])
    return None


def _filter_payload_timestamps(topic: str, data: Any, cutoff: datetime) -> Optional[Any]:
    """Filter stale entries from payloads that contain timestamps.

    Returns filtered data, or None if the entire message should be dropped.
    """
    if not isinstance(data, dict):
        return data

    if topic == "RaceControlMessages":
        messages = data.get("Messages")
        if isinstance(messages, dict):
            filtered = {}
            for k, msg in messages.items():
                if isinstance(msg, dict):
                    utc = _parse_utc(msg.get("Utc", ""))
                    if utc and utc < cutoff:
                        continue
                filtered[k] = msg
            if not filtered:
                return None
            return {"Messages": filtered}
        elif isinstance(messages, list):
            filtered = []
            for m in messages:
                if not isinstance(m, dict):
                    filtered.append(m)
                    continue
                utc = _parse_utc(m.get("Utc", ""))
                if utc and utc < cutoff:
                    continue
                filtered.append(m)
            if not filtered:
                return None
            return {"Messages": filtered}

    elif topic == "SessionData":
        result = {}
        for key in ("Series", "StatusSeries"):
            section = data.get(key)
            if section is None:
                continue
            if isinstance(section, dict):
                filtered = {}
                for k, entry in section.items():
                    if isinstance(entry, dict):
                        utc = _parse_utc(entry.get("Utc", ""))
                        if utc and utc < cutoff:
                            continue
                    filtered[k] = entry
                if filtered:
                    result[key] = filtered
            elif isinstance(section, list):
                filtered = [e for e in section
                            if not isinstance(e, dict) or not _parse_utc(e.get("Utc", "")) or _parse_utc(e.get("Utc", "")) >= cutoff]
                if filtered:
                    result[key] = filtered
        return result if result else None

    return data


class SessionPreProcessor:
    """Transforms raw JSONL into pre-computed display data in SQLite."""

    def __init__(self, session_path: Path, session_type: str):
        self._session_path = session_path
        self._session_type = session_type
        self._expected_key = _extract_session_key(session_path.name)

        self._db = SessionDatabase(session_path)
        self._bus = SessionMessageBus()
        self._processors: list[Processor] = []

        self._output_buffer: list[tuple[int, str, str]] = []  # (offset, topic, json)
        self._last_emitted: dict[str, str] = {}  # topic -> last JSON string
        self._start_time: Optional[datetime] = None
        self._cutoff: Optional[datetime] = None  # 1h before reference time
        self._running = False
        self._message_count = 0
        self._last_flush_ms = 0

        # Gating state
        self._gated = True  # True until SessionInfo with correct key arrives
        self._gate_buffer: list[RawMessage] = []
        self._gate_first_ts: Optional[datetime] = None

        # Set to break out of tail-follow so the run can finalize.
        self._stop_follow = asyncio.Event()

        # Topic discovery — alert on raw topics no processor handles.
        self._known_topics, self._first_topic_run = _load_known_topics()
        self._checked_topics: set = set()       # topics inspected this run
        self._unprocessed_seen: set = set()     # seen this run with no handler
        self._known_topics_dirty = False

    async def run(
        self,
        tail_follow: bool = False,
        on_progress: Optional[Callable[[float], None]] = None,
        on_caught_up: Optional[Callable[[], None]] = None,
        on_baseline_ready: Optional[Callable[[], None]] = None,
        force: bool = False,
    ) -> None:
        self._running = True
        # Reprocess (offline --force): delete the existing DB and recreate it
        # fresh, so the rebuild never inherits the old file's page allocation
        # (a pre-redesign DB is mostly stale free pages — ~10x larger). Live /
        # tail-follow builds keep their growing DB (reset() clears rows).
        if force and not tail_follow:
            base = self._db._db_path
            for suffix in ("", "-wal", "-shm"):
                base.with_name(base.name + suffix).unlink(missing_ok=True)
        self._db.open()

        status = self._db.get_meta("status")
        if status == "complete" and not tail_follow and not force:
            logger.info(f"Session already processed: {self._session_path.name}")
            if on_progress:
                on_progress(100.0)
            if on_baseline_ready:
                on_baseline_ready()
            return

        # Fresh build — clear any prior (partial or stale) rows so a
        # re-run cannot duplicate data (the whole JSONL is re-read).
        self._db.reset()
        self._db.set_meta("status", "processing")

        # Detect session type from subscribe.json metadata (harmless — just for type)
        initial_state = load_subscribe_json(self._session_path)
        session_info = initial_state.get("SessionInfo", {})
        if not self._session_type:
            si_type = (session_info.get("Type") or "").lower()
            si_name = (session_info.get("Name") or "").lower()
            if "qualifying" in si_type or "qualifying" in si_name or "shootout" in si_type:
                self._session_type = "qualifying"
            elif si_type in ("race", "sprint"):
                self._session_type = "race"
            else:
                self._session_type = "practice"

        # If no expected key from folder name, try subscribe.json
        if self._expected_key is None:
            self._expected_key = session_info.get("Key")

        # Scheduled session start (UTC) — used to suppress pre-session scrubber
        # `event` markers. Sourced from the SessionInfoProcessor's emitted
        # `sessionInfo` (derived from live.jsonl), NOT subscribe.json (a capture
        # snapshot that can reflect a later state). Set on the first emit (below).
        self._scheduled_start_utc: Optional[datetime] = None

        self._init_processors()
        self._bus.set_persist_sink(self._capture_output)

        total_lines = 0
        if on_progress:
            live_file = self._session_path / "live.jsonl"
            if live_file.exists():
                with open(live_file, "r", encoding="utf-8") as f:
                    total_lines = sum(1 for _ in f)

        lines_processed = 0

        try:
            def _on_caught_up():
                self._flush_buffer()
                if on_caught_up:
                    on_caught_up()

            async for msg in read_jsonl(
                self._session_path,
                fast=not tail_follow,
                tail_follow=tail_follow,
                on_caught_up=_on_caught_up if tail_follow else None,
                stop_follow=self._stop_follow if tail_follow else None,
            ):
                if not self._running:
                    break

                lines_processed += 1

                # Skip stale telemetry at file start
                if self._start_time is None and msg.topic in ("CarData.z", "Position.z"):
                    continue

                # --- Gating: buffer until SessionInfo with correct key ---
                if self._gated:
                    if not self._gate_buffer:
                        self._gate_first_ts = msg.timestamp

                    # Check if this is the SessionInfo we're waiting for
                    if msg.topic == "SessionInfo" and isinstance(msg.data, dict):
                        msg_key = msg.data.get("Key")
                        if msg_key is not None and msg_key == self._expected_key:
                            self._gated = False
                            self._start_time = msg.timestamp
                            self._cutoff = msg.timestamp - timedelta(hours=1)
                            logger.info(f"Session gated: key={msg_key} at {msg.timestamp}")

                            # Emit SessionInfo first
                            self._bus.emit(msg.topic, msg.data, msg.timestamp)
                            self._message_count += 1

                            # Flush buffer:
                            #  - DriverList: always flush, so the driver-list
                            #    processor has full team info regardless of
                            #    where the TeamName update sits in the
                            #    archive's timestamp stream (downloads merge
                            #    per-topic .jsonStream files in time order;
                            #    F1 may have sent the team-info DriverList
                            #    before SessionInfo).
                            #  - Other topics: only those with the same
                            #    timestamp as SessionInfo (filters stale
                            #    pre-session timing data).
                            ref_ts = msg.timestamp
                            for buffered in self._gate_buffer:
                                if buffered is msg:
                                    continue
                                if not (buffered.timestamp == ref_ts
                                        or buffered.topic == "DriverList"):
                                    continue
                                filtered = self._filter_message(buffered)
                                if filtered:
                                    self._discover_topic(filtered.topic)
                                    self._bus.emit(filtered.topic, filtered.data, filtered.timestamp)
                                    self._message_count += 1
                            self._gate_buffer = []
                            # Baseline (SessionInfo + DriverList + same-ts
                            # topics) is now emitted. Flush so a reader
                            # connection sees the offset-0 rows, then signal
                            # baseline-ready — the engine gates its connect
                            # restore on this so tiles paint without a manual
                            # seek (card 77).
                            self._flush_buffer()
                            if on_baseline_ready:
                                on_baseline_ready()
                            continue

                    # Buffer the message
                    self._gate_buffer.append(msg)

                    # Timeout: discard buffer if no matching SessionInfo within 60s
                    if (self._gate_first_ts and
                            (msg.timestamp - self._gate_first_ts).total_seconds() > GATE_TIMEOUT_S):
                        logger.warning(f"Gate timeout: discarding {len(self._gate_buffer)} buffered messages")
                        self._gate_buffer = []
                        self._gate_first_ts = msg.timestamp

                    continue

                # --- Normal processing (after gate opened) ---
                filtered = self._filter_message(msg)
                if not filtered:
                    continue

                self._message_count += 1
                offset_ms = int((filtered.timestamp - self._start_time).total_seconds() * 1000)

                self._discover_topic(filtered.topic)
                self._bus.emit(filtered.topic, filtered.data, filtered.timestamp)

                if (self._message_count % BUFFER_FLUSH_MESSAGES == 0
                        or offset_ms - self._last_flush_ms >= BUFFER_FLUSH_MS):
                    self._flush_buffer()
                    self._last_flush_ms = offset_ms

                if on_progress and total_lines > 0 and lines_processed % 1000 == 0:
                    on_progress(min(99.0, (lines_processed / total_lines) * 100))

                if self._message_count % YIELD_EVERY == 0:
                    await asyncio.sleep(0)

            # lap_classification needs no end-of-session flush — the rewritten
            # processor reclassifies live (and Rule 1 retroactively), so the
            # latest emitted state is already correct.

            # End-of-session flush for the telemetry processor — emits
            # the in-flight IN lap that may not have triggered a PIT
            # transition before the recording ended (non-race only),
            # and emits empty-placeholder rows up to NL_max so every
            # lap has a telem entry.
            if self._telem_proc is not None and self._start_time is not None:
                last_dt = filtered.timestamp if filtered else self._start_time
                try:
                    self._telem_proc.finalize_session(last_dt)
                except Exception:
                    logger.exception("Telemetry processor finalize_session failed")

            # Pecking-order emission — runs AFTER lap reclassification
            # is finalised so PUSH / LONG / COOL labels are stable. The
            # final _flush_buffer() persists the peckingOrder row to DB.
            self._flush_buffer()
            # (End-of-session lap reclassification removed: the
            # lap_classification_processor reclassifies retroactively as
            # each lap time arrives, so the live snapshot is already
            # correct.)
            if self._pace_proc:
                try:
                    self._pace_proc.save_analysis()
                except Exception:
                    logger.exception("Pace processor save_analysis failed")
                # Post-session analyses that consume pace.json: tyre
                # phases (depends only on session.db) + pecking order
                # (depends on this session's pace + prior chain). Run
                # both after pace.json is written.
                try:
                    from app.analysis.tyre_phases import analyze_and_save as _tyre_save
                    _tyre_save(self._session_path)
                except Exception:
                    logger.exception("Tyre phase analysis failed")
                try:
                    from app.analysis.pecking_order import compute_and_save as _po_save
                    _po_save(self._session_path)
                except Exception:
                    logger.exception("Pecking-order analysis failed")
                # Strategy prediction runs only at Qualifying finalize
                # (= predicts the upcoming Race).
                try:
                    from app.analysis.strategy_prediction import (
                        compute_and_save as _sp_save, _is_qualifying,
                    )
                    if _is_qualifying(self._session_path):
                        _sp_save(self._session_path)
                except Exception:
                    logger.exception("Strategy prediction failed")
                # Strategy validation runs at Race finalize (= compares
                # actual race strategies to the Q session's prediction).
                try:
                    from app.analysis.strategy_validation import (
                        compute_and_save as _sv_save, _is_race,
                    )
                    if _is_race(self._session_path):
                        _sv_save(self._session_path)
                except Exception:
                    logger.exception("Strategy validation failed")
            self._flush_buffer()
            self._db.set_meta("status", "complete")
            self._db.set_meta("message_count", str(self._message_count))

            if on_progress:
                on_progress(100.0)

            logger.info(
                f"Pre-processing complete: {self._message_count} messages, "
                f"{self._session_path.name}"
            )

        except asyncio.CancelledError:
            logger.info(f"Pre-processing cancelled: {self._session_path.name}")
            raise
        except Exception:
            logger.exception(f"Pre-processing error: {self._session_path.name}")
            self._db.set_meta("status", "error")

    def _filter_message(self, msg: RawMessage) -> Optional[RawMessage]:
        """Apply timestamp filtering to a message. Returns None to drop it."""
        if self._cutoff is None:
            return msg

        # Envelope timestamp check
        if msg.timestamp.tzinfo is None:
            msg_utc = msg.timestamp.replace(tzinfo=timezone.utc)
        else:
            msg_utc = msg.timestamp
        cutoff_aware = self._cutoff if self._cutoff.tzinfo else self._cutoff.replace(tzinfo=timezone.utc)

        if msg_utc < cutoff_aware:
            return None

        # Payload timestamp filtering for specific topics
        if msg.topic in ("RaceControlMessages", "SessionData"):
            filtered_data = _filter_payload_timestamps(msg.topic, msg.data, cutoff_aware)
            if filtered_data is None:
                return None
            return RawMessage(topic=msg.topic, data=filtered_data, timestamp=msg.timestamp)

        return msg

    async def stop(self) -> None:
        """Request a graceful stop.

        Breaks tail-follow at the next EOF so `run()` finalizes the DB
        (status='complete') instead of being left mid-build.
        """
        self._running = False
        self._stop_follow.set()

    def close(self) -> None:
        self._persist_known_topics(force=True)
        self._db.close()

    def _init_processors(self) -> None:
        telem_proc = TelemetryProcessor(self._bus, self._session_type)
        self._telem_proc = telem_proc  # stash for end-of-session finalize
        # Pace processor — COMMENTED OUT (placeholder, to be reintroduced /
        # reworked as pace_prediction). While disabled, the finalize block
        # gated on `if self._pace_proc:` (pace.json + tyre_phases +
        # pecking_order + strategy_prediction) is skipped.
        # from app.processing.processors.pace_processor import PaceProcessor
        # self._pace_proc = PaceProcessor(
        #     self._bus, self._session_type,
        #     session_path=self._session_path,
        #     session_name=self._session_path.name,
        # )
        self._pace_proc = None
        # LapClassificationProcessor's TimingData handler MUST run
        # BEFORE DriverStatusProcessor's (= so _timing_lap is updated
        # before driverStatus is emitted to wildcard subscribers).
        # Otherwise _on_driver_status at pit exit fires with stale
        # _timing_lap and emits the wrong lap number (off-by-one in
        # P/Q, lap 2 missing in classification).
        self._lap_class_proc = LapClassificationProcessor(self._bus, self._session_type)
        self._processors = [
            SessionInfoProcessor(self._bus, self._session_type),
            ClockProcessor(self._bus, self._session_type),
            ChampionshipProcessor(self._bus, self._session_type),
            DriverListProcessor(self._bus, self._session_type),
            self._lap_class_proc,
            DriverStatusProcessor(self._bus, self._session_type),
            LapTimingProcessor(self._bus, self._session_type),
            DriverGapProcessor(self._bus, self._session_type),
            SectorTimingProcessor(self._bus, self._session_type),
            TyreProcessor(self._bus, self._session_type),
            StandingsProcessor(self._bus, self._session_type),
            RacePaceProcessor(self._bus, self._session_type),
            RaceControlProcessor(self._bus, self._session_type),
            # FIA Stewards stack — only meaningful for race + sprint;
            # the processor itself no-ops if registered elsewhere.
            *([FiaStewardsProcessor(self._bus, self._session_type)]
              if self._session_type in ("race", "sprint") else []),
            PositionProcessor(self._bus, self._session_type),
            telem_proc,
            LapDeltaProcessor(self._bus, self._session_type),
            LapPredictionProcessor(self._bus, self._session_type),
            TrackStatusProcessor(self._bus, self._session_type),
            WeatherProcessor(self._bus, self._session_type),
            TeamRadioProcessor(self._bus, self._session_type),
            DataHealthProcessor(self._bus, self._session_type),
            HeartbeatProcessor(self._bus, self._session_type),
            # self._pace_proc,  # COMMENTED OUT — pace placeholder (see _init above)
        ]
        for p in self._processors:
            p.skip_animations = True
            p.subscribe()
        # Scheduled start (for pre-session scrubber-event suppression) comes from
        # the SessionInfoProcessor's emitted sessionInfo (live.jsonl-derived),
        # registered AFTER the processors so its emit reaches this handler.
        self._bus.on("sessionInfo", self._on_session_info)

    def _on_session_info(self, data: Any, clock_time: datetime) -> None:
        """Derive the scheduled start (UTC) from the emitted sessionInfo
        (startDate local + gmtOffset, both from live.jsonl). Once only —
        startDate is static session metadata."""
        if self._scheduled_start_utc is not None or not isinstance(data, dict):
            return
        sd = data.get("startDate")
        gmt = data.get("gmtOffset") or "00:00:00"
        if not sd:
            return
        try:
            local_dt = datetime.fromisoformat(sd.replace("Z", "").split("+")[0].split(".")[0])
            sign = -1 if str(gmt).startswith("-") else 1
            h, m, s = (int(x) for x in str(gmt).lstrip("-+").split(":"))
            off = timedelta(seconds=sign * (h * 3600 + m * 60 + s))
            self._scheduled_start_utc = (local_dt - off).replace(tzinfo=timezone.utc)
        except Exception:
            logger.warning(f"Could not parse sessionInfo.startDate for {self._session_path.name}")

    def _discover_topic(self, topic: str) -> None:
        """Track raw topics; alert on genuinely-new ones no processor handles.

        Runs for both live (tail-follow) and replay. First run (no baseline)
        seeds silently; afterwards a new topic with no specific bus handler
        logs a warning and fires a dev notification so a processor can be
        added. New-but-already-handled topics are just logged.
        """
        if topic in self._checked_topics:
            return
        self._checked_topics.add(topic)
        processed = self._bus.has_subscriber(topic)
        if not processed:
            self._unprocessed_seen.add(topic)
        if topic in self._known_topics:
            return

        # First time this topic has ever been seen.
        self._known_topics.add(topic)
        self._known_topics_dirty = True
        self._persist_known_topics()
        if self._first_topic_run:
            return  # seeding the baseline — don't alert
        if processed:
            logger.info(f"Topic discovery: new topic '{topic}' (already handled)")
            return
        logger.warning(
            f"Topic discovery: NEW UNPROCESSED topic '{topic}' — no processor "
            f"handles it ({self._session_path.name})"
        )
        try:
            from app.notifications import send_notification
            send_notification(
                "F1Unleashed: new unprocessed topic",
                f"'{topic}' arrived in {self._session_path.name} but no "
                f"processor handles it.",
                priority="high", tags="warning",
            )
        except Exception:
            logger.exception("Topic-discovery notification failed")

    def _session_rel(self) -> str:
        """This session's 'year/event/session' tail for the catalog's lastSeen."""
        try:
            from app.config import CACHE_DIR
            return str(self._session_path.relative_to(CACHE_DIR))
        except (ValueError, OSError):
            return self._session_path.name

    def _persist_known_topics(self, force: bool = False) -> None:
        """Write the topic CATALOG (card 120): every known topic with how we use
        it — status, the processors that listen, the outputs that processing
        produces, whether it's captured to live.jsonl, and when it was last seen.
        Listeners come from the bus handler map; outputs are derived at runtime
        (bus._io_outputs). Notes are user-editable and preserved across runs."""
        if not (self._known_topics_dirty or force):
            return
        try:
            prev = {}
            if KNOWN_TOPICS_FILE.exists():
                try:
                    loaded = _json.loads(KNOWN_TOPICS_FILE.read_text())
                    if isinstance(loaded, dict):
                        prev = loaded
                except (ValueError, OSError):
                    pass

            raw = set(RAW_F1_TOPICS)
            sess = self._session_rel()

            # Listeners per topic: processor class names with a specific handler.
            listeners: dict[str, list] = {}
            for topic, handlers in self._bus._handlers.items():
                if topic == "*":
                    continue
                names = sorted({
                    type(getattr(h, "__self__", None)).__name__
                    for h in handlers if getattr(h, "__self__", None) is not None
                })
                if names:
                    listeners[topic] = names

            topics = raw | set(listeners) | set(self._known_topics)
            catalog: dict[str, dict] = {}
            for topic in sorted(topics):
                if topic == "*":
                    continue
                seen = topic in self._checked_topics
                if topic in listeners:
                    status = "subscribed"
                elif seen or topic in self._known_topics:
                    status = "received"      # arrived but no processor handles it
                else:
                    status = "unseen"        # known baseline, absent this session
                p = prev.get(topic, {}) if isinstance(prev.get(topic), dict) else {}
                last_seen = max(sess, p.get("lastSeen", "")) if seen else p.get("lastSeen", "")
                catalog[topic] = {
                    "status": status,
                    "listeners": listeners.get(topic, []),
                    # Collapse per-driver outputs (driverGap:1, driverGap:10, …)
                    # to their base name (driverGap) for a readable catalog.
                    "outputs": sorted({o.split(":")[0] for o in self._bus._io_outputs.get(topic, [])}),
                    "captured": topic in raw,
                    "lastSeen": last_seen,
                    "note": p.get("note", ""),
                }

            KNOWN_TOPICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            KNOWN_TOPICS_FILE.write_text(_json.dumps(catalog, indent=2, sort_keys=True))
            self._known_topics_dirty = False
        except OSError:
            logger.exception("Failed to persist topic catalog")

    def _capture_output(self, topic: str, data: Any, clock_time: datetime) -> None:
        """Wildcard handler to capture processor output for DB."""
        if topic in RAW_F1_TOPICS:
            return
        if topic.startswith("~"):
            return
        if self._start_time is None:
            return

        # Scrubber-event filter — suppress any `event` scrubber marker (from
        # TrackStatusProcessor: implicit-GREEN at pit-exit-open, etc.) that
        # fires BEFORE the scheduled session start, so pre-session noise stays
        # off the scrubber. Threshold = SessionInfo's scheduled start (UTC).
        if topic == "event" and self._scheduled_start_utc is not None \
                and clock_time < self._scheduled_start_utc:
            return

        offset_ms = int((clock_time - self._start_time).total_seconds() * 1000)
        # Wall-clock string (HH:MM:SS.SSS) for human-readable cross-
        # referencing in DB exports.
        wall_clock = clock_time.strftime("%H:%M:%S.") + f"{clock_time.microsecond // 1000:03d}"

        # Completed-lap telemetry (telemetryLap:{driver}:{lap}) is persisted as
        # a normal message row, fetched on demand by topic.

        json_str = _json.dumps(data, default=str)
        if self._last_emitted.get(topic) == json_str:
            return
        self._last_emitted[topic] = json_str
        # Tuple order matches DB column order: offset_ms, wall_clock, topic, data.
        self._output_buffer.append((offset_ms, wall_clock, topic, json_str))

    def _flush_buffer(self) -> None:
        if not self._output_buffer:
            return
        self._db.save_messages(self._output_buffer)
        self._output_buffer = []
