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
from app.processing.processors.playback_event_processor import PlaybackEventProcessor
from app.processing.processors.position_processor import PositionProcessor
from app.processing.processors.race_control_processor import RaceControlProcessor
from app.processing.processors.fia_stewards_processor import FiaStewardsProcessor
from app.processing.processors.race_laps_processor import RaceLapsProcessor
from app.processing.processors.session_data_processor import SessionDataProcessor
from app.processing.processors.session_info_processor import SessionInfoProcessor
from app.processing.processors.standings_processor import StandingsProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor
from app.processing.processors.timing_processor import TimingProcessor
from app.processing.processors.lap_timing_processor import LapTimingProcessor
from app.processing.processors.driver_gap_processor import DriverGapProcessor
from app.processing.processors.sector_timing_processor import SectorTimingProcessor
from app.processing.processors.track_status_processor import TrackStatusProcessor
from app.processing.processors.weather_processor import WeatherProcessor
from app.processing.lap_reclassification import reclassify_session

logger = logging.getLogger(__name__)

BUFFER_FLUSH_MESSAGES = 100
BUFFER_FLUSH_MS = 1000
YIELD_EVERY = 2000
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


def is_testing_session(session_path: Path) -> bool:
    """True for F1 pre-season testing sessions (folder name 'Day_N').

    Testing sessions run ~8 h and are rarely replayed, so they are
    exempt from eager session.db building (backfill / download) — a DB
    is built on demand only if such a session is actually opened.
    """
    name = session_path.name
    parts = name.split("_", 1)
    if parts[0].isdigit() and len(parts) > 1:
        name = parts[1]
    return name.startswith("Day_")


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
        self._playback_event_proc: Optional[PlaybackEventProcessor] = None
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

    async def run(
        self,
        tail_follow: bool = False,
        on_progress: Optional[Callable[[float], None]] = None,
        on_caught_up: Optional[Callable[[], None]] = None,
        force: bool = False,
    ) -> None:
        self._running = True
        self._db.open()

        status = self._db.get_meta("status")
        if status == "complete" and not tail_follow and not force:
            logger.info(f"Session already processed: {self._session_path.name}")
            if on_progress:
                on_progress(100.0)
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

        # Parse the SCHEDULED session start (UTC). SessionInfo.StartDate
        # is in track-local time without timezone; GmtOffset gives the
        # local-vs-UTC delta. Used to filter scrubber events: anything
        # before scheduled start is suppressed (pre-session noise). The
        # threshold can be moved EARLIER by a brought-forward RCM.
        self._scheduled_start_utc: Optional[datetime] = None
        try:
            sd = session_info.get("StartDate")
            gmt = session_info.get("GmtOffset") or "00:00:00"
            if sd:
                local_dt = datetime.fromisoformat(
                    sd.replace("Z", "").split("+")[0].split(".")[0]
                )
                sign = -1 if str(gmt).startswith("-") else 1
                h, m, s = (int(x) for x in str(gmt).lstrip("-+").split(":"))
                off = timedelta(seconds=sign * (h * 3600 + m * 60 + s))
                self._scheduled_start_utc = (local_dt - off).replace(tzinfo=timezone.utc)
        except Exception:
            logger.warning(f"Could not parse SessionInfo.StartDate for {self._session_path.name}")

        self._init_processors()
        if self._scheduled_start_utc and self._playback_event_proc:
            self._playback_event_proc.set_effective_start_utc(self._scheduled_start_utc)
        self._bus.on("*", self._capture_output)

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

                            # Emit sessionStart playback event
                            self._playback_event_proc.emit_session_start(msg.timestamp)

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
                                    self._bus.emit(filtered.topic, filtered.data, filtered.timestamp)
                                    self._message_count += 1
                            self._gate_buffer = []
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

                self._bus.emit(filtered.topic, filtered.data, filtered.timestamp)

                if (self._message_count % BUFFER_FLUSH_MESSAGES == 0
                        or offset_ms - self._last_flush_ms >= BUFFER_FLUSH_MS):
                    self._flush_buffer()
                    self._last_flush_ms = offset_ms

                if on_progress and total_lines > 0 and lines_processed % 1000 == 0:
                    on_progress(min(99.0, (lines_processed / total_lines) * 100))

                if self._message_count % YIELD_EVERY == 0:
                    await asyncio.sleep(0)

            # Emit sessionEnd playback event
            if self._playback_event_proc and self._start_time:
                last_ts = filtered.timestamp if filtered else self._start_time
                self._playback_event_proc.emit_session_end(last_ts)

            # Pre-start marker (5 min before scheduled session start).
            # Done at finalize so the offset_ms math has self._start_time
            # set (captured by the first message earlier).
            if self._playback_event_proc and self._start_time:
                self._playback_event_proc.maybe_emit_pre_start(self._start_time)

            # Audio first-audible marker — scan commentary*.aac for the
            # last `silence_end` of the leading silence and emit an
            # event there so the user can jump straight to the first
            # audible moment. Cheap (~50 ms per file with silencedetect).
            self._emit_audio_first_sound_marker()

            # End-of-session flush for the timing processor — fills
            # lap_times[K] = None for each lap K from 1 to NL_max that
            # didn't receive an LL value (= covers the trailing
            # in-progress lap whose time F1 never published because NL
            # never bumped past it). Must run BEFORE telem.finalize so
            # the empty-placeholder fill in telem sees the full lap
            # count (including the in-progress lap).
            if self._timing_proc is not None and self._start_time is not None:
                last_dt = filtered.timestamp if filtered else self._start_time
                try:
                    self._timing_proc.finalize_session(last_dt)
                except Exception:
                    logger.exception("Timing processor finalize_session failed")

            # End-of-session flush for lap_classification — ensures every
            # lap 1..NL_max has a classification entry (trailing in-progress
            # lap gets IN if no explicit classification was emitted).
            if self._lap_class_proc is not None and self._start_time is not None:
                last_dt = filtered.timestamp if filtered else self._start_time
                try:
                    self._lap_class_proc.finalize_session(last_dt)
                except Exception:
                    logger.exception("LapClassif finalize_session failed")

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
            # _finalize_lap_reclassification: DISABLED 2026-06-03.
            # The end-of-session reclassifier was needed by the OLD
            # live classifier which couldn't see future lap times. The
            # NEW lap_classification_processor does proper retroactive
            # reclass when each lap_time arrives, so the live snapshot
            # is already correct. Running the old reclassifier here
            # corrupts race classifications (= it doesn't know about
            # the RACE state and overwrites with PUSH/LONG/COOL).
            # self._finalize_lap_reclassification()
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

    def _emit_audio_first_sound_marker(self) -> None:
        """Scan the earliest commentary*.aac segment for the end of the
        leading silence (silencedetect noise=-50dB d=2) and emit an
        `audioStart` playbackEvent at the corresponding clock_time.

        Maps audio offset → clock_time via the segment's audio_info
        start_utc (written by live_capture at ffmpeg launch). Skipped
        when there's no audio file, no audio_info, or no silence at all.
        """
        if not (self._playback_event_proc and self._start_time):
            return
        try:
            aacs = sorted(self._session_path.glob("commentary*.aac"))
            if not aacs:
                return
            # Earliest segment = lowest sortable name. commentary.001.aac
            # sorts BEFORE commentary.aac alphabetically — pick the first.
            first = aacs[0]
            # Find the matching audio_info: commentary.001.aac → audio_info.001.json,
            # commentary.aac → audio_info.json.
            stem = first.stem  # "commentary" or "commentary.001"
            if stem == "commentary":
                info_path = self._session_path / "audio_info.json"
            else:
                # stem like "commentary.001"
                suffix = stem.split(".", 1)[1]
                info_path = self._session_path / f"audio_info.{suffix}.json"
            if not info_path.exists():
                return
            info = _json.loads(info_path.read_text())
            start_utc_str = info.get("start_utc")
            if not start_utc_str:
                return
            audio_start = datetime.fromisoformat(
                start_utc_str.replace("Z", "+00:00")
            )
            import subprocess
            res = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "info",
                 "-i", str(first),
                 "-af", "silencedetect=noise=-50dB:d=2",
                 "-f", "null", "-"],
                capture_output=True, text=True, timeout=120,
            )
            first_silence_end = None
            for line in (res.stderr or "").splitlines():
                if "silence_end:" in line:
                    try:
                        first_silence_end = float(
                            line.split("silence_end:")[1].split()[0]
                        )
                        break
                    except (ValueError, IndexError):
                        pass
            if first_silence_end is None or first_silence_end <= 0:
                return
            audible_utc = audio_start + timedelta(seconds=first_silence_end)
            self._playback_event_proc.emit_audio_start(audible_utc)
        except Exception:
            logger.exception("Audio first-sound marker failed")

    def _finalize_lap_reclassification(self) -> None:
        """End-of-session pass: re-evaluate every lap's classification
        against the whole session's lap-time pattern (see
        ``app.processing.lap_reclassification``) and overwrite the live
        classifier's per-driver ``lapClassification:NN`` topics with
        the corrected ``laps`` snapshot. Downstream consumers read the
        latest snapshot, so the corrections take effect."""
        import json as _json
        import sqlite3 as _sqlite3

        if self._db._conn is None:
            return

        # Latest classification snapshot per driver. Also carry forward
        # the per-lap qualifying-segment map so the rewrite below
        # preserves it (otherwise the finalize row drops `lapSegments`
        # and the frontend's segment-grouped lap-list breaks).
        latest_cls: dict[str, dict[int, str]] = {}
        latest_lap_segments: dict[str, dict[str, int]] = {}
        for topic, data in self._db._conn.execute(
            "SELECT topic, data FROM messages "
            "WHERE topic LIKE 'lapClassification:%' "
            "AND rowid IN (SELECT MAX(rowid) FROM messages "
            "             WHERE topic LIKE 'lapClassification:%' GROUP BY topic)"
        ):
            num = topic.split(":", 1)[1]
            try:
                payload = _json.loads(data)
            except _json.JSONDecodeError:
                continue
            laps_map = payload.get("laps")
            if isinstance(laps_map, dict):
                latest_cls[num] = {int(k): v for k, v in laps_map.items()}
            lap_segs = payload.get("lapSegments")
            if isinstance(lap_segs, dict):
                latest_lap_segments[num] = dict(lap_segs)

        if not latest_cls:
            return

        # Latest lap times per driver.
        lap_times: dict[str, dict[int, int]] = {}
        for topic, data in self._db._conn.execute(
            "SELECT topic, data FROM messages "
            "WHERE topic LIKE 'driverLapTimes:%' "
            "AND rowid IN (SELECT MAX(rowid) FROM messages "
            "             WHERE topic LIKE 'driverLapTimes:%' GROUP BY topic)"
        ):
            num = topic.split(":", 1)[1]
            try:
                payload = _json.loads(data)
            except _json.JSONDecodeError:
                continue
            per_drv: dict[int, int] = {}
            for lap_str, t_str in payload.items():
                try:
                    lap = int(lap_str)
                except ValueError:
                    continue
                ms = _parse_lap_time_ms(t_str)
                if ms is not None:
                    per_drv[lap] = ms
            lap_times[num] = per_drv

        # Build per-driver lap records for the reclassifier.
        per_driver: dict[str, list[dict]] = {}
        for num, cls_map in latest_cls.items():
            recs: list[dict] = []
            for lap, status in sorted(cls_map.items()):
                recs.append({
                    "lap": lap,
                    "lap_time_ms": lap_times.get(num, {}).get(lap),
                    "current_class": status,
                })
            per_driver[num] = recs

        corrected = reclassify_session(per_driver)

        # Diff and write new lapClassification snapshots at offset_ms = max+1
        # so the "latest snapshot" rule picks up the corrections.
        max_offset = self._db._conn.execute(
            "SELECT COALESCE(MAX(offset_ms), 0) FROM messages"
        ).fetchone()[0]
        finalize_offset = int(max_offset) + 1
        changed = 0
        rows = []
        for num, new_map in corrected.items():
            old_map = latest_cls.get(num, {})
            if new_map == old_map:
                continue
            for lap, st in new_map.items():
                if old_map.get(lap) != st:
                    changed += 1
            lap_segs = latest_lap_segments.get(num, {})
            payload = {
                "lap": max(new_map),
                "status": new_map[max(new_map)],
                "segment": int(lap_segs.get(str(max(new_map)), 0) or 0),
                "laps": {str(k): v for k, v in new_map.items()},
                "lapSegments": lap_segs,
            }
            rows.append((finalize_offset, f"lapClassification:{num}",
                         _json.dumps(payload, default=str)))
        if rows:
            self._db._conn.executemany(
                "INSERT INTO messages (offset_ms, topic, data) VALUES (?, ?, ?)",
                rows,
            )
            self._db._conn.commit()
        logger.info(
            "Lap reclassification: %d drivers, %d lap labels updated",
            len(rows), changed,
        )

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
        self._db.close()

    def _init_processors(self) -> None:
        self._playback_event_proc = PlaybackEventProcessor(self._bus, self._session_type)
        telem_proc = TelemetryProcessor(self._bus, self._session_type)
        self._telem_proc = telem_proc  # stash for end-of-session finalize
        # Pace processor — per-session team quali + race pace extraction.
        # Writes pace.json to data/analysis/{year}/{event}/{session}/ at
        # finalize. Does NOT emit on the bus; consumers (race-control
        # tile, cohort detection) read previous events' analysis files.
        from app.processing.processors.pace_processor import PaceProcessor
        self._pace_proc = PaceProcessor(
            self._bus, self._session_type,
            session_path=self._session_path,
            session_name=self._session_path.name,
        )
        # LapClassificationProcessor's TimingData handler MUST run
        # BEFORE DriverStatusProcessor's (= so _timing_lap is updated
        # before driverStatus is emitted to wildcard subscribers).
        # Otherwise _on_driver_status at pit exit fires with stale
        # _timing_lap and emits the wrong lap number (off-by-one in
        # P/Q, lap 2 missing in classification).
        self._timing_proc = TimingProcessor(self._bus, self._session_type)
        self._lap_class_proc = LapClassificationProcessor(self._bus, self._session_type)
        self._processors = [
            SessionInfoProcessor(self._bus, self._session_type),
            ClockProcessor(self._bus, self._session_type),
            ChampionshipProcessor(self._bus, self._session_type),
            DriverListProcessor(self._bus, self._session_type),
            SessionDataProcessor(self._bus, self._session_type),
            self._lap_class_proc,
            DriverStatusProcessor(self._bus, self._session_type),
            self._timing_proc,
            LapTimingProcessor(self._bus, self._session_type),
            DriverGapProcessor(self._bus, self._session_type),
            SectorTimingProcessor(self._bus, self._session_type),
            StandingsProcessor(self._bus, self._session_type),
            RaceControlProcessor(self._bus, self._session_type),
            # FIA Stewards stack — only meaningful for race + sprint;
            # the processor itself no-ops if registered elsewhere.
            *([FiaStewardsProcessor(self._bus, self._session_type)]
              if self._session_type in ("race", "sprint") else []),
            *([RaceLapsProcessor(self._bus, self._session_type)]
              if self._session_type == "race" else []),
            PositionProcessor(self._bus, self._session_type),
            telem_proc,
            LapPredictionProcessor(self._bus, self._session_type, telemetry_processor=telem_proc),
            TrackStatusProcessor(self._bus, self._session_type),
            WeatherProcessor(self._bus, self._session_type),
            self._pace_proc,
            self._playback_event_proc,  # Must be last — listens to other processors' output
        ]
        for p in self._processors:
            p.skip_animations = True
            p.subscribe()

    def _capture_output(self, topic: str, data: Any, clock_time: datetime) -> None:
        """Wildcard handler to capture processor output for DB."""
        if topic in RAW_F1_TOPICS:
            return
        if topic.startswith("~"):
            return
        if self._start_time is None:
            return

        # Scrubber-event filter — suppress any scrubber-bound event
        # (`event` from TrackStatusProcessor; also `playbackEvent`
        # markers other than sessionStart/sessionEnd/preStart5min/
        # audioStart) that happens BEFORE scheduled session start. The
        # threshold may have been shifted earlier by a brought-forward
        # RCM (handled by the playback event processor).
        if topic == "event" and self._playback_event_proc \
                and self._playback_event_proc.filter_event(clock_time):
            return

        offset_ms = int((clock_time - self._start_time).total_seconds() * 1000)
        # Wall-clock string (HH:MM:SS.SSS) for human-readable cross-
        # referencing in DB exports.
        wall_clock = clock_time.strftime("%H:%M:%S.") + f"{clock_time.microsecond // 1000:03d}"

        # Lap telemetry goes to its own table, queried on demand.
        if topic.startswith("lapTelemetry:"):
            parts = topic.split(":")
            if len(parts) == 3:
                driver, lap_str = parts[1], parts[2]
                try:
                    lap = int(lap_str)
                except ValueError:
                    return
                # Each lapTelemetry payload is a list of samples; each
                # sample is [dp, speed, rpm, gear, thr, brk, t_ms_rel].
                # t_ms_rel is relative to lap start, so the lap start
                # wall-clock = clock_time - last_sample.t_ms_rel.
                end_wall_clock = wall_clock
                if isinstance(data, list) and data:
                    from datetime import timedelta
                    start_ts = clock_time - timedelta(milliseconds=int(data[-1][6]))
                    start_wall_clock = (
                        start_ts.strftime("%H:%M:%S.")
                        + f"{start_ts.microsecond // 1000:03d}"
                    )
                else:
                    start_wall_clock = end_wall_clock  # empty placeholder lap
                self._db.save_telemetry(
                    driver, lap, offset_ms,
                    _json.dumps(data, default=str),
                    start_wall_clock=start_wall_clock,
                    end_wall_clock=end_wall_clock,
                )
            return

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
