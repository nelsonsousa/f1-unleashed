"""
Session Pre-Processor — transforms raw JSONL into pre-computed display data.

Message gating (2026-08-17-047 WB-1 resume — completes the 2026-07-27-003
pipeline redesign's §9.2 universal gate, DECISIONS.md #1):
  - `StreamNormalizer` (constructed with a real `scheduled_start_utc`, see
    `SessionPreProcessor.__init__`) discards any message whose payload
    timestamp is more than 60 minutes before the session's SCHEDULED start,
    before this module ever sees it. This is the ONLY gate now — the old
    `SessionInfo.Key` exact-match buffer/gate and the old pre-start `.z` skip
    are both removed, replaced by this one rule.
  - The first message to survive that gate becomes this module's own
    `_start_time`/reference (mirrors `StreamNormalizer._stamp()`'s own
    "first survivor" auto-reference, requirement-spec.md §9.2). The baseline
    flush + `on_baseline_ready` trigger separately (fix for M1,
    review-findings.md, 2026-08-17-047 WB-1 resume): on the first message
    strictly LATER than `_start_time` (i.e. once the opening same-timestamp
    group — which routinely includes the real baseline topics, SessionInfo-
    derived data and DriverList — has fully arrived and been emitted), with
    an end-of-run fallback for a session where that never happens (see
    `run()`).
  - After that, messages with envelope timestamps >1h before `_start_time`
    are filtered out by `_filter_message` — a separate, already-shipped
    mechanism (architecture-plan.md §A.7.7), not the universal gate above.

Payload timestamp filtering:
  - For topics like RaceControlMessages, SessionData, etc., individual entries with
    payload timestamps (e.g. Messages.*.Utc) before the cutoff are stripped.
"""

import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

_PROC_VERSION: Optional[str] = None

# Grace window on the scrubber-event pre-session filter (IIYYPBxZ, see
# `_capture_output`): comfortably exceeds the ~±150ms STREAM_LAG estimation
# residual observed on live captures (census range -0.089s to +0.232s across
# 40 non-Race sessions, 2026-08-20 investigation), while staying well short of
# the genuine pre-session noise the filter was built to suppress (an early
# SessionStatus=Started at pit-lane-open, measured 12-55 minutes before the
# scheduled start; the two known genuinely-early outliers, -25.9s and
# -1795.6s, are both intentionally left suppressed by this margin).
_SCRUBBER_EVENT_GRACE = timedelta(seconds=30)


def processor_code_version() -> str:
    """A short hash of the processing code (all processors + this preprocessor).
    It changes whenever any processor's source changes, so a transient DB built by
    OLDER code is not reused on reconnect — replay always runs the latest code (M1).
    Cached after first computation."""
    global _PROC_VERSION
    if _PROC_VERSION is None:
        h = hashlib.sha1()
        # Includes stream_normalizer.py and file_reader.py (architecture-plan.md
        # §A.7.6): a change confined to either would otherwise not bump this
        # hash, so an existing transient DB would be silently reused despite
        # the computed timestamps having changed.
        files = (sorted((Path(__file__).parent / "processors").glob("*.py"))
                 + [Path(__file__),
                    Path(__file__).parent / "stream_normalizer.py",
                    Path(__file__).parent / "file_reader.py"])
        for f in files:
            try:
                h.update(f.read_bytes())
            except OSError:
                pass
        _PROC_VERSION = h.hexdigest()[:16]
    return _PROC_VERSION

import json as _json

from app.config import DATA_DIR
from app.processing.database import SessionDatabase
from app.processing.file_reader import read_jsonl, load_subscribe_json
from app.processing.stream_normalizer import StreamNormalizer, NormalizedMessage
from app.processing.dp_reckoner import DpReckoner
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
from app.processing.processors.pq_pace_processor import PQPaceProcessor
from app.processing.processors.sector_colour_processor import SectorColourProcessor
from app.processing.processors.best_sector_processor import BestSectorProcessor
from app.processing.processors.team_radio_processor import TeamRadioProcessor
from app.processing.processors.data_health_processor import DataHealthProcessor
from app.processing.processors.heartbeat_processor import HeartbeatProcessor
from app.processing.processors.pit_stop_loss_processor import PitStopLossProcessor
from app.processing.processors.dashboard_info_processor import DashboardInfoProcessor
from app.processing.processors.dashboard_autoselect_processor import DashboardAutoSelectProcessor
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


async def run_end_of_session_analysis(session_path: Path) -> None:
    """Run the end-of-session analyses (FP-based pecking-order + geometric
    pit-loss estimate) for a session, offloaded to threads so they never block
    the event loop. Idempotent — each compute_and_save overwrites its JSON — so
    it is safe to call at the live SessionStatus=Ends, again at capture finalize,
    and at end-of-download. Reads the (transient) session DB, so that DB must
    exist first. (card 6a636a46)"""
    try:
        from app.analysis.pecking_order import compute_and_save as _po_save
        await asyncio.to_thread(_po_save, session_path)
    except Exception:
        logger.exception("Pecking-order analysis failed")
    try:
        from app.analysis.pit_loss_estimate import compute_and_save as _ple_save
        await asyncio.to_thread(_ple_save, session_path)
    except Exception:
        logger.exception("Pit-loss estimate analysis failed")


class SessionPreProcessor:
    """Transforms raw JSONL into pre-computed display data in SQLite."""

    def __init__(self, session_path: Path, session_type: str, *,
                 scheduled_start_utc: Optional[datetime] = None):
        self._session_path = session_path
        self._session_type = session_type

        self._db = SessionDatabase(session_path)
        self._bus = SessionMessageBus()
        self._processors: list[Processor] = []
        self.failed = False   # set True if run() hits an unhandled error (H5)

        # StreamNormalizer (2026-07-27-003 pipeline redesign, completed
        # 2026-08-17-047 WB-1 resume): owns STREAM_LAG, .z decompression/
        # splitting, the continuous dedup rule (§9.1), and now the universal
        # 60-minute-before-scheduled-start gate (§9.2) — the ONLY gate,
        # replacing the old SessionInfo.Key exact-match buffer/gate entirely
        # (DECISIONS.md #1's completion; see `run()` for where `_start_time`/
        # the baseline flush now anchor off the first gate-surviving message
        # instead of a Key match).
        #
        # `scheduled_start_utc` is a CONSTRUCTOR PARAMETER threaded through
        # from the caller (session.py / livetiming_fetcher.py / live_capture.py
        # — see file-impact-map.md §1 for each call site's source) and is NOT
        # the same value as `self._scheduled_start_utc` below, which
        # `_on_session_info` sets later from the PROCESSED `sessionInfo` bus
        # emission purely to suppress pre-session scrubber `event` markers.
        # Two different values, two different purposes, that happen to share
        # a name (file-impact-map.md §1.4) — never conflate them.
        self._normalizer = StreamNormalizer(scheduled_start_utc=scheduled_start_utc)

        self._output_buffer: list[tuple[int, str, str]] = []  # (offset, topic, json)
        self._last_emitted: dict[str, str] = {}  # topic -> last JSON string
        self._start_time: Optional[datetime] = None
        self._cutoff: Optional[datetime] = None  # 1h before reference time
        # True once `on_baseline_ready` has fired for this run — set once the
        # first timestamp group past `_start_time` has closed (or, failing
        # that, at end of run — see `run()`), guaranteed to fire at most once
        # (item 3, 2026-08-17-047 WB-1 resume; M1 fix, review-findings.md).
        self._baseline_ready_fired = False
        # N9 hardening (review-findings.md addendum, 2026-08-18-051): the
        # opening timestamp group is NOT guaranteed to contain `SessionInfo`
        # -- only true of all 21 golden fixtures this project holds, enforced
        # nowhere in the code. `_session_info_seen`/`_session_info_ts` track
        # the moment the DERIVED `sessionInfo` bus topic (SessionInfoProcessor,
        # not the raw envelope topic) is actually processed, wherever in the
        # stream that falls, so `on_baseline_ready`'s trigger can wait for
        # SessionInfo's OWN timestamp group to close instead of assuming it
        # shares the opening one. Set from `_on_session_info` below.
        self._session_info_seen = False
        self._session_info_ts: Optional[datetime] = None
        self._running = False
        self._message_count = 0
        self._last_flush_ms = 0
        # AC-3's wall-clock backstop (requirement-spec.md): the latest
        # timestamp released via `poll_wall_clock_backstop`, merged into
        # `last_ts` at end of `run()`. Initialized here (not just in run())
        # so `_wall_clock_backstop_loop`/`_emit_flushed_message` are safe to
        # call independently of a full `run()`.
        self._latest_backstop_ts: Optional[datetime] = None

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
        on_caught_up: Optional[Callable[[], None]] = None,
        on_baseline_ready: Optional[Callable[[], None]] = None,
        force: bool = False,
        pace: bool = False,
        speed: float = 1.0,
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

        # Scheduled session start (UTC) — used to suppress pre-session scrubber
        # `event` markers. Sourced from the SessionInfoProcessor's emitted
        # `sessionInfo` (derived from live.jsonl), NOT subscribe.json (a capture
        # snapshot that can reflect a later state). Set on the first emit (below).
        self._scheduled_start_utc: Optional[datetime] = None

        self._init_processors()
        self._bus.set_persist_sink(self._capture_output)

        last_ts = None   # timestamp of the last emitted message; drives finalize
        self._latest_backstop_ts: Optional[datetime] = None
        backstop_task: Optional[asyncio.Task] = None

        try:
            def _on_caught_up():
                self._flush_buffer()
                if on_caught_up:
                    on_caught_up()

            # AC-3's wall-clock backstop (requirement-spec.md; file-impact-
            # map.md §1): the reorder buffer's message-driven watermark check
            # never fires during a genuine WHOLE-FEED stall (nothing arrives
            # at all, of any topic — not just CarData.z/Position.z). Only
            # meaningful where real wall-clock time can actually elapse
            # between messages (live tail-follow, or a paced replay); a fast
            # offline replay has no waiting to guard against, and the
            # end-of-stream flush above picks up whatever remains at EOF
            # regardless.
            if tail_follow or pace:
                backstop_task = asyncio.create_task(self._wall_clock_backstop_loop())
                # Give the freshly-created task a chance to actually start
                # (asyncio.create_task only SCHEDULES it; nothing guarantees
                # it runs before the main loop below, which can complete
                # entirely synchronously for a very short/fast build) before
                # any code that might cancel it runs.
                await asyncio.sleep(0)

            async for msg in self._normalizer.normalize(read_jsonl(
                self._session_path,
                fast=not tail_follow,
                tail_follow=tail_follow,
                on_caught_up=_on_caught_up if tail_follow else None,
                stop_follow=self._stop_follow if tail_follow else None,
                pace=pace,
                speed=speed,
            )):
                if not self._running:
                    break

                # The universal gate (StreamNormalizer._gate(), §9.2) has
                # already dropped anything more than 60 minutes before the
                # scheduled start — everything reaching this point has
                # survived it. The FIRST survivor anchors `_start_time`/
                # `_cutoff` (the new equivalent of the old SessionInfo.Key
                # gate-open moment; DECISIONS.md #1's completion) — set here,
                # unconditionally, on the first message this loop ever sees,
                # rather than deferred to after `_filter_message` below, so a
                # message that itself gets filtered out (rare: RCM/SessionData
                # payload-entry filtering, not the envelope-level cutoff,
                # which a message can never fail against a cutoff computed
                # FROM itself) doesn't leave `_start_time`/`_cutoff` unset.
                if self._start_time is None:
                    self._start_time = msg.utc_timestamp
                    # Persist the absolute session start so the engine can
                    # anchor its playback clock from the DB instead of a
                    # separate pre-scan of live.jsonl (KdKK0D5G).
                    self._db.set_meta("start_time", msg.utc_timestamp.isoformat())
                    self._cutoff = msg.utc_timestamp - timedelta(hours=1)
                    logger.info(f"Universal gate opened at {msg.utc_timestamp}")

                filtered = self._filter_message(msg)
                if not filtered:
                    continue

                self._message_count += 1
                last_ts = filtered.utc_timestamp
                # Taken FROM the message (architecture-plan.md §A.1.2) — the
                # normalizer already computed it relative to ITS OWN
                # first-survivor reference (StreamNormalizer._stamp()), which
                # is exactly this same first message, so no explicit
                # `set_reference()` override is needed any more (DECISIONS.md
                # #2's completion).
                offset_ms = filtered.offset_ms if filtered.offset_ms is not None else int(
                    (filtered.utc_timestamp - self._start_time).total_seconds() * 1000)

                self._discover_topic(filtered.topic)
                self._bus.emit(filtered.topic, filtered.data, filtered.utc_timestamp)

                if (not self._baseline_ready_fired
                        and self._session_info_seen
                        and filtered.utc_timestamp > self._session_info_ts):
                    # Baseline ready (M1 fix, review-findings.md,
                    # 2026-08-17-047 WB-1 resume; N9 hardening,
                    # 2026-08-18-051): fire once SessionInfo's OWN timestamp
                    # group closes -- i.e. once SessionInfo has actually been
                    # processed (`_session_info_seen`) AND a message strictly
                    # later than ITS timestamp (`_session_info_ts`) arrives --
                    # not once the OPENING group closes regardless of whether
                    # SessionInfo was in it (N9: nothing enforces that;
                    # `_session_info_ts` decouples the two). This is still the
                    # direct, stateless analogue of the old SessionInfo.Key
                    # gate's behaviour ("emit SessionInfo, then flush the
                    # buffered same-timestamp batch, then signal"): a real
                    # capture's other baseline topics (DriverList) routinely
                    # share SessionInfo's own envelope timestamp with other
                    # no-op topics (e.g. TrackStatus "AllClear" against a
                    # fresh processor -- review's reproduction against
                    # regression/golden/shanghai-sq-cdn), so waiting for the
                    # first LATER timestamp after SessionInfo's own guarantees
                    # every same-timestamp message -- baseline topics
                    # included -- has already been emitted and buffered
                    # before we flush and signal. When SessionInfo happens to
                    # share `_start_time`'s exact timestamp (all 21 golden
                    # fixtures), `_session_info_ts == _start_time` and this
                    # reduces exactly to the prior "opening group closes"
                    # behaviour.
                    # (A session where SessionInfo never arrives, or arrives
                    # but nothing ever follows it, never reaches this branch;
                    # the end-of-run fallback below covers both.)
                    self._baseline_ready_fired = True
                    self._flush_buffer()
                    self._last_flush_ms = offset_ms
                    if on_baseline_ready:
                        on_baseline_ready()
                elif (self._message_count % BUFFER_FLUSH_MESSAGES == 0
                        or offset_ms - self._last_flush_ms >= BUFFER_FLUSH_MS):
                    self._flush_buffer()
                    self._last_flush_ms = offset_ms

                if self._message_count % YIELD_EVERY == 0:
                    await asyncio.sleep(0)

            # lap_classification needs no end-of-session flush — the rewritten
            # processor reclassifies live (and Rule 1 retroactively), so the
            # latest emitted state is already correct.

            # Stop the wall-clock backstop poller (if running) before the
            # explicit flush below — both would otherwise touch the
            # normalizer's buffer concurrently, and its job is done once the
            # message loop above has exited.
            if backstop_task is not None:
                backstop_task.cancel()
                try:
                    await backstop_task
                except asyncio.CancelledError:
                    pass
                backstop_task = None
            if self._latest_backstop_ts is not None and (
                    last_ts is None or self._latest_backstop_ts > last_ts):
                last_ts = self._latest_backstop_ts

            # AC-3 (requirement-spec.md; file-impact-map.md §1's correction):
            # release whatever CarData.z/Position.z entries the reorder
            # buffer is still holding at end of stream. Hooked to exhaustion
            # of the async iterator above (works for BOTH offline EOF and
            # live tail-follow's stop_follow-triggered break — see
            # file-impact-map.md §1), NOT the inert `_SessionEnd` marker.
            # Must run BEFORE finalize_session, below: finalize_session
            # closes whatever lap is still open using whatever is in
            # drv.samples AT THAT MOMENT, so a still-buffered `.z` entry
            # flushed afterward would be exactly the "vanishes silently"
            # case this fix exists to close.
            # `self._start_time is not None` alone now captures "gate has
            # opened" — the old separate `_gated` flag is gone (DECISIONS.md
            # #1's completion); `_start_time` IS the gate-open signal, set at
            # the same point `_gated` used to flip False (see the main loop
            # above).
            if self._start_time is not None:
                for msg in self._normalizer.flush():
                    if not self._running:
                        break
                    ts = self._emit_flushed_message(msg)
                    if ts is not None and (last_ts is None or ts > last_ts):
                        last_ts = ts

            # End-of-session flush for the telemetry processor — emits
            # the in-flight IN lap that may not have triggered a PIT
            # transition before the recording ended (non-race only),
            # and emits empty-placeholder rows up to NL_max so every
            # lap has a telem entry.
            if self._telem_proc is not None and self._start_time is not None:
                last_dt = last_ts or self._start_time
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
            # (The dormant pace/tyre-phases/strategy analysis stack was removed — M2.
            # The live pecking-order + pit-loss-estimate analyses run below/elsewhere.)
            self._flush_buffer()

            # Baseline-ready end-of-run fallback (M1, review-findings.md,
            # 2026-08-17-047 WB-1 resume; N9 hardening, 2026-08-18-051). The
            # in-loop trigger above only fires once SessionInfo has been
            # processed AND a message strictly later than ITS timestamp
            # arrives — two shapes never reach that branch: (a) SessionInfo
            # never appears at all in this capture, or (b) SessionInfo
            # arrives but is the LAST thing to ever land (its own group never
            # closes -- includes the single-message-session and
            # every-message-shares-one-timestamp cases). Fire here instead,
            # after every flush above has already run, so the callback still
            # fires (at most once) for any session that emitted at least one
            # message — the same "flush strictly before signal" guarantee,
            # just at the other end of the run instead of mid-loop. Note this
            # fires even when SessionInfo never arrived at all (shape (a)):
            # there is no baseline left to wait for, and a connecting client
            # would otherwise wait forever.
            if (self._start_time is not None and not self._baseline_ready_fired
                    and self._message_count > 0):
                self._baseline_ready_fired = True
                if on_baseline_ready:
                    on_baseline_ready()

            self._db.set_meta("status", "complete")
            self._db.set_meta("message_count", str(self._message_count))
            self._db.set_meta("processor_version", processor_code_version())
            # Normalizer counters (architecture-plan.md §A.8 item 1) — the
            # ground truth for how many messages per topic were forwarded vs.
            # dedup-suppressed vs. gate-dropped, needed for AC-6'/AC-8-style
            # cross-source count comparisons.
            self._db.set_meta("normalizer_counters", _json.dumps(self._normalizer.counters))
            self._db.set_meta("stream_lag_final", str(self._normalizer.stream_lag_s))

            # End-of-session analyses (FP pecking-order + geometric pit-loss),
            # offloaded. Extracted to run_end_of_session_analysis so the live
            # SessionStatus=Ends path and end-of-download reuse the same chain
            # (B03 lBuRgUm9 offload; card 6a636a46 shared trigger).
            await run_end_of_session_analysis(self._session_path)

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
            self.failed = True   # surfaced to the client by the caller (H5)
            # Re-raise so a caller that doesn't explicitly check `.failed`
            # (e.g. LiveTimingFetcher.fetch_session) cannot mistake a build
            # that failed partway through for a successful one (WB-4 / R1).
            raise
        finally:
            # Safety net for any exit path (including an exception above)
            # that skipped the normal cancellation point — a leaked
            # backstop task would otherwise keep polling a normalizer whose
            # owning run() has already exited.
            if backstop_task is not None and not backstop_task.done():
                backstop_task.cancel()

    async def _wall_clock_backstop_loop(self) -> None:
        """AC-3's wall-clock backstop poll loop (requirement-spec.md; see
        the call site in `run()` for when this is started). Polls
        `StreamNormalizer.poll_wall_clock_backstop()` on a fixed interval,
        well below the backstop duration itself, and feeds anything released
        through the same per-message handling `_emit_flushed_message` uses.
        This is the ONE non-content-driven path in the whole pipeline —
        see `StreamNormalizer.poll_wall_clock_backstop`'s docstring for the
        determinism carve-out this corresponds to."""
        interval_s = 5.0
        while self._running:
            await asyncio.sleep(interval_s)
            try:
                released = self._normalizer.poll_wall_clock_backstop()
            except Exception:
                logger.exception("StreamNormalizer wall-clock backstop poll failed")
                continue
            if not released:
                continue
            # The buffer this releases from holds ONLY CarData.z/Position.z
            # entries (StreamNormalizer._z_buffer — see poll_wall_clock_
            # backstop's docstring). Every entry in it has ALREADY survived
            # the universal gate (StreamNormalizer._gate() runs before
            # insertion, §9.2) — genuinely pre-session zombies are dropped
            # there now, not here (DECISIONS.md #1's completion; the old
            # `_gated` buffer this comment used to describe no longer
            # exists). This guard is retained only as a defensive backstop
            # for the (essentially unreachable in practice — the backstop
            # only fires after `wall_clock_backstop_s`, default 180s, of
            # whole-feed silence) edge case where it fires before the main
            # loop has processed even its own first message and set
            # `_start_time`/`_cutoff`: `_start_time is None` mirrors exactly
            # what the main loop itself hasn't established yet.
            if self._start_time is None:
                continue
            for msg in released:
                ts = self._emit_flushed_message(msg)
                if ts is not None and (self._latest_backstop_ts is None
                                        or ts > self._latest_backstop_ts):
                    self._latest_backstop_ts = ts
            self._flush_buffer()

    def _emit_flushed_message(self, msg: NormalizedMessage) -> Optional[datetime]:
        """Shared per-message handling for a `NormalizedMessage` released
        OUTSIDE the main per-line loop in `run()` — AC-3's end-of-stream
        flush and wall-clock backstop. Mirrors the main loop body's
        filter/count/discover/emit steps. Returns the message's
        `utc_timestamp` if it was actually emitted, else None (dropped by
        `_filter_message`)."""
        filtered = self._filter_message(msg)
        if not filtered:
            return None
        self._message_count += 1
        self._discover_topic(filtered.topic)
        self._bus.emit(filtered.topic, filtered.data, filtered.utc_timestamp)
        return filtered.utc_timestamp

    def _filter_message(self, msg: NormalizedMessage) -> Optional[NormalizedMessage]:
        """Apply timestamp filtering to a message. Returns None to drop it.

        Compares against `utc_timestamp` (the normalizer's STREAM_LAG-corrected
        timestamp), not the raw envelope — a deliberate behaviour improvement
        for `.z` topics (architecture-plan.md §A.7.7): it is exactly the
        payload-timestamp variant the prior zombie-message investigation
        (CVFyRpfx) found missing from the old envelope-only cutoff.
        """
        if self._cutoff is None:
            return msg

        if msg.utc_timestamp.tzinfo is None:
            msg_utc = msg.utc_timestamp.replace(tzinfo=timezone.utc)
        else:
            msg_utc = msg.utc_timestamp
        cutoff_aware = self._cutoff if self._cutoff.tzinfo else self._cutoff.replace(tzinfo=timezone.utc)

        if msg_utc < cutoff_aware:
            return None

        # Payload timestamp filtering for specific topics. This runs IN
        # ADDITION to the normalizer's own continuous dedup rule (§9.1) — the
        # two are complementary, not redundant: dedup drops exact resent
        # repeats/already-seen entries by key/index; this drops entries that
        # are merely stale relative to the session's 1h cutoff, dedup or not.
        if msg.topic in ("RaceControlMessages", "SessionData"):
            filtered_data = _filter_payload_timestamps(msg.topic, msg.data, cutoff_aware)
            if filtered_data is None:
                return None
            return NormalizedMessage(topic=msg.topic, data=filtered_data,
                                      envelope_ts=msg.envelope_ts,
                                      utc_timestamp=msg.utc_timestamp,
                                      offset_ms=msg.offset_ms)

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
        # WB3 (docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/,
        # requirement-spec.md AC-5, "single reckoner, not two"): PositionProcessor
        # and TelemetryProcessor share the SAME DpReckoner instance, not two
        # independently-calibrated copies.
        dp_reckoner = DpReckoner()
        telem_proc = TelemetryProcessor(self._bus, self._session_type, reckoner=dp_reckoner)
        self._telem_proc = telem_proc  # stash for end-of-session finalize
        # (The old per-session PaceProcessor + pace/tyre-phases/strategy analysis stack
        # was removed — M2. To be rebuilt later. The live pace colours come from the
        # Race/PQ pace processors below.)
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
            PQPaceProcessor(self._bus, self._session_type),
            SectorColourProcessor(self._bus, self._session_type),
            BestSectorProcessor(self._bus, self._session_type),
            RaceControlProcessor(self._bus, self._session_type),
            # FIA Stewards stack — only meaningful for race + sprint;
            # the processor itself no-ops if registered elsewhere.
            *([FiaStewardsProcessor(self._bus, self._session_type)]
              if self._session_type in ("race", "sprint") else []),
            # WB3: PositionProcessor MUST be registered (and therefore
            # dispatched, SessionMessageBus.on() calls same-topic handlers in
            # registration order) BEFORE telem_proc on CarData.z --
            # telem_proc's own _handle_car_data relies on PositionProcessor
            # having already advanced the shared dp_reckoner for the
            # batch's last entry before it reads current_dp() for that same
            # tick (dp_reckoner.py's "IMPORTANT for WB3" docstring note;
            # file-impact-map.md §8.1/§4 item 1). Same precedent as the
            # LapClassificationProcessor/DriverStatusProcessor ordering
            # comment above.
            PositionProcessor(self._bus, self._session_type, reckoner=dp_reckoner),
            telem_proc,
            LapDeltaProcessor(self._bus, self._session_type),
            LapPredictionProcessor(self._bus, self._session_type),
            TrackStatusProcessor(self._bus, self._session_type),
            WeatherProcessor(self._bus, self._session_type),
            TeamRadioProcessor(self._bus, self._session_type),
            DataHealthProcessor(self._bus, self._session_type),
            HeartbeatProcessor(self._bus, self._session_type),
            # In-race pit-stop time-loss measurement — races only (needs driverInt/gap +
            # the F1 pit topics). No-ops in P/Q (subscribe() early-returns).
            *([PitStopLossProcessor(self._bus, self._session_type)]
              if self._session_type == "race" else []),
            # Live-dashboard per-driver info tile — P/Q only (subscribe() early-returns otherwise).
            DashboardInfoProcessor(self._bus, self._session_type),
            # Dashboard auto-select recommendation (card wfMzaSwh) — all session types.
            DashboardAutoSelectProcessor(self._bus, self._session_type),
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
        startDate is static session metadata.

        Also records the FIRST moment the derived `sessionInfo` topic is
        processed (N9 hardening, 2026-08-18-051) -- unconditionally, ahead of
        the early returns below, so `on_baseline_ready`'s trigger in `run()`
        knows exactly when SessionInfo's own timestamp group opened, even if
        `_scheduled_start_utc` was already set by an earlier emission or this
        particular payload can't be parsed for it."""
        if not self._session_info_seen:
            self._session_info_seen = True
            self._session_info_ts = clock_time
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
        # off the scrubber. Threshold = SessionInfo's scheduled start (UTC),
        # minus a grace window (IIYYPBxZ, 2026-08-20 investigation): for
        # Practice/Qualifying the session-start green flag IS the scheduled
        # start (F1 publishes SessionStatus=Started within ~50-230ms of it),
        # and `clock_time` here is `envelope_ts - STREAM_LAG`, an ESTIMATE
        # carried over from a different topic (ExtrapolatedClock) that is
        # accurate to only ~±150ms on live captures — so comparing it against
        # the bare threshold decided by coin flip whether the single most
        # important scrubber marker survived (measured: 12.5% of non-Race
        # sessions, 20% of FP, lost it). The grace is sized to comfortably
        # exceed that clock-residual noise while staying far short of the
        # genuine pre-session noise this filter exists to suppress (an early
        # SessionStatus=Started at pit-lane-open, measured 12-55 minutes
        # before the scheduled start in held captures) — it does not exempt
        # the marker outright, which would reopen exactly that hole.
        if topic == "event" and self._scheduled_start_utc is not None \
                and clock_time < self._scheduled_start_utc - _SCRUBBER_EVENT_GRACE:
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
