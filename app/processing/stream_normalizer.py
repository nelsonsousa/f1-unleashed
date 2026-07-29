"""
StreamNormalizer — the pre-processing stage of the SignalR-to-transient-database
pipeline redesign.

Reads: `docs/artifacts/2026-07-27-003-pipeline-redesign/requirement-spec.md` §9
(DESIGN CLOSED — the authoritative decisions) and `architecture-plan.md` §A.3
(this module's design). Where this docstring or the code below ever appears to
disagree with `requirement-spec.md` §9, that spec wins — this is the
implementation of it, not a second source of truth.

Owns, in one place, everything `file_reader.py` used to conflate with "read
the file": the causal `STREAM_LAG`/`REFERENCE_UTC_TIMESTAMP` timestamp
correction, `.z` decompression/splitting, the universal 60-minutes-before-
scheduled-start gate (superseding the old `SessionInfo.Key` gate and the old
pre-start `.z` skip — both replaced by one rule, §9.2), and the continuous
dedup rule that replaces reconnect-marker/burst-window handling entirely
(§9.1 — there is no "in-burst" concept in the closed design at all).

Determinism (a hard constraint, `rules/data-processing.md` + requirement-spec
§4): every branch below depends only on the current line and on state derived
from strictly earlier lines. No wall clock, no randomness, no external I/O,
no lookahead. Replaying the same file twice must produce identical output.
"""

from __future__ import annotations

import base64
import bisect
import json
import logging
import time as _time_module
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Callable, Optional

from app.processing.file_reader import RawLine, _parse_timestamp

logger = logging.getLogger(__name__)

# Synthetic marker topics that must never reach a processor or _discover_topic.
# No `_Reconnect`/`_ReconnectEnd` marker is written today (§9.1 dropped the
# marker design entirely) — this set exists so that IF a future capture ever
# carries one (e.g. hand-authored test fixtures, or a reintroduced marker),
# the normalizer silently swallows it rather than alerting on an "unprocessed
# topic" or feeding it to a processor as real data. See DECISIONS.md #1.
SYNTHETIC_TOPICS = {"_SessionEnd", "_Reconnect", "_ReconnectEnd"}

# .z topics bypass STREAM_LAG entirely and use their own payload timestamp.
Z_TOPICS = {"CarData.z", "Position.z"}

# The reference topic STREAM_LAG is computed from. Also bypasses STREAM_LAG
# for its OWN utcTimestamp, but not via a special case: updating STREAM_LAG
# before computing utc_timestamp makes envelope - (envelope - payload) fall
# out to payload automatically (architecture-plan.md §A.3.2 step 3).
REFERENCE_TOPIC = "ExtrapolatedClock"

# Cumulative-list topics: the resent-history case the continuous dedup rule
# (§9.1) filters by key/index high-water mark rather than by content hash.
# This is the closed design's own list (architecture-plan.md §A.3.3), not a
# generic "any array-shaped payload" auto-detection — see DECISIONS.md #4.
CUMULATIVE_LIST_TOPICS = {"RaceControlMessages", "SessionData"}

# Universal gate window (§9.2): discard any message whose payload timestamp
# is earlier than this long before the session's SCHEDULED start.
GATE_WINDOW = timedelta(minutes=60)

# AC-1: the bounded reorder buffer's window, scoped to CarData.z/Position.z
# only. Validated at full-session scale across 27 sessions/7 events
# (requirement-spec.md AC-1) -- do not change without re-validating per that
# artifact trail.
Z_REORDER_WINDOW = timedelta(seconds=1.0)

# AC-3: default wall-clock backstop duration. No source artifact validates a
# specific value (requirement-spec.md Open Question #1) -- this is sized
# conservatively as a safety net, well above the worst observed whole-feed
# stall (87.3s, Miami Race), not as a tuned parameter. See
# implementation-plan.md for the full reasoning.
DEFAULT_WALL_CLOCK_BACKSTOP_S = 180.0


def decompress_z_data(data: str) -> Any:
    """Decompress .z topic data (base64 -> zlib raw inflate -> JSON).

    Moved verbatim from file_reader.py (architecture-plan.md §A.6) — no logic
    change.
    """
    decoded = base64.b64decode(data)
    decompressed = zlib.decompress(decoded, -zlib.MAX_WBITS)
    return json.loads(decompressed)


def split_z_entries(topic: str, data: Any) -> list[tuple[Optional[str], Any]]:
    """Split a decompressed .z message into individual entries with payload
    timestamps. Returns list of (iso_timestamp_or_None, single_entry_data).

    Moved verbatim from file_reader.py — no logic change.
    """
    if topic == "CarData.z" and isinstance(data, dict) and "Entries" in data:
        result = []
        for entry in data["Entries"]:
            utc = entry.get("Utc")
            if utc:
                result.append((utc, {"Entries": [entry]}))
        return result if result else [(None, data)]

    if topic == "Position.z" and isinstance(data, dict) and "Position" in data:
        result = []
        for pos in data["Position"]:
            ts = pos.get("Timestamp")
            if ts:
                result.append((ts, {"Position": [pos]}))
        return result if result else [(None, data)]

    return [(None, data)]


@dataclass
class NormalizedMessage:
    """One message forwarded downstream, with a trustworthy computed
    timestamp and (once a reference exists) an offset relative to it."""
    topic: str
    data: Any
    envelope_ts: datetime
    utc_timestamp: datetime
    offset_ms: Optional[int]


def _entry_index_keys(topic: str, data: Any) -> Optional[dict[str, Any]]:
    """Return {key_str: entry} for a cumulative-list topic's indexed
    structure, or None if the payload doesn't have one this rule understands.

    RaceControlMessages: Json.Messages, a dict keyed "0","1",... (or, per the
    existing preprocessor.py filter, sometimes a list — normalized to a dict
    keyed by position for a uniform high-water mark).
    SessionData: Json.Series / Json.StatusSeries, same shape.
    """
    if not isinstance(data, dict):
        return None
    if topic == "RaceControlMessages":
        section = data.get("Messages")
        return _as_keyed_dict(section)
    if topic == "SessionData":
        # A SessionData message can carry Series and/or StatusSeries. Treat
        # each independently isn't representable in one flat dict without key
        # collisions, so this rule inspects StatusSeries first, then Series,
        # preferring whichever is present (closed-design fixtures — the
        # Budapest Q proof, requirement-spec.md §9.1 — exercise StatusSeries;
        # Series follows the identical shape). See DECISIONS.md #5.
        for key in ("StatusSeries", "Series"):
            section = data.get(key)
            keyed = _as_keyed_dict(section)
            if keyed is not None:
                return keyed
    return None


def _as_keyed_dict(section: Any) -> Optional[dict[str, Any]]:
    if isinstance(section, dict):
        return section
    if isinstance(section, list):
        return {str(i): entry for i, entry in enumerate(section)}
    return None


def _rebuild_cumulative_payload(topic: str, original: Any, kept: dict[str, Any]) -> Any:
    """Rebuild a cumulative-list topic's payload from a filtered key->entry map,
    preserving the original container shape (dict-of-entries)."""
    if topic == "RaceControlMessages":
        return {"Messages": kept}
    if topic == "SessionData":
        # Re-wrap under whichever section key the entries came from.
        if isinstance(original, dict) and isinstance(original.get("StatusSeries"), (dict, list)):
            return {"StatusSeries": kept}
        return {"Series": kept}
    return original


def _sort_key(key: str):
    """Numeric sort when every key is an integer string (the common case);
    falls back to lexicographic so an unexpected non-numeric key never raises."""
    try:
        return (0, int(key))
    except (TypeError, ValueError):
        return (1, key)


class StreamNormalizer:
    """Computes a trustworthy per-message `utcTimestamp`/`offsetMs`, applies
    the universal session-start gate, and de-duplicates reconnect-resent
    content — all as one causal, single-pass, no-lookahead stage.

    Constructible and drivable without a database (architecture-plan.md
    §A.3.1/§A.8 item 3): feed it an async iterator of `RawLine` (or call
    `process_line` directly with synthetic input) and it needs nothing else.
    """

    def __init__(self, *, scheduled_start_utc: Optional[datetime] = None,
                 wall_clock_backstop_s: Optional[float] = DEFAULT_WALL_CLOCK_BACKSTOP_S,
                 _now: Optional[Callable[[], float]] = None):
        self._scheduled_start_utc = scheduled_start_utc
        self._gate_cutoff = (
            scheduled_start_utc - GATE_WINDOW if scheduled_start_utc is not None else None
        )
        self._stream_lag: timedelta = timedelta(0)
        self._reference_utc: Optional[datetime] = None

        # AC-1/AC-2: the bounded 1.0s reorder buffer, scoped ONLY to
        # CarData.z/Position.z (the only two topics ever routed through
        # _process_z). Holds raw (utc_ts, topic, entry_data, envelope_ts,
        # arrival_wall) tuples -- gate/dedup already applied at insert time
        # (unrelated to ordering, kept exactly where it always ran);
        # `_stamp()` (which mutates `_reference_utc` on first call) is
        # deferred to RELEASE time, not insert time, per the design note in
        # file-impact-map.md §1. Kept sorted by utc_ts via bisect.insort.
        self._z_buffer: list[tuple[datetime, str, Any, datetime, float]] = []
        # AC-2's hard constraint: this watermark is written ONLY from
        # `_insert_z`, which is only ever called from `_process_z` on a
        # CarData.z/Position.z entry -- never from `_process_generic`.
        self._z_watermark: Optional[datetime] = None

        # AC-3: wall-clock backstop for the whole-feed-silence case (no
        # message of ANY topic arrives, so the message-driven watermark
        # check above never fires at all). Injectable clock, following this
        # codebase's own established pattern (file_reader.py's `_now`/
        # `_sleep` injection for `pace`). `None` disables the backstop
        # entirely (used by callers/tests that don't need it).
        self._wall_clock_backstop_s = wall_clock_backstop_s
        self._now: Callable[[], float] = _now or _time_module.monotonic

        # Whether the continuous dedup rule (§9.1) is currently applied. A
        # caller that buffers messages BEFORE deciding whether they will
        # actually be forwarded downstream (e.g. preprocessor.py's
        # SessionInfo.Key gate-buffer, DECISIONS.md #1) must disable dedup
        # for that buffering window via `set_dedup_enabled(False)` — otherwise
        # a message that is later discarded at gate-flush can still
        # consume/poison the dedup state, silently suppressing the REAL
        # subsequent message for that topic even though nothing was actually
        # emitted yet. See DECISIONS.md / verification-report for this
        # regression's history. Enabled by default so standalone use (tests,
        # a caller with no separate buffering stage) gets dedup as documented.
        self._dedup_enabled: bool = True

        # Continuous dedup state (§9.1).
        self._last_emitted_json: dict[str, str] = {}       # singleton topics: last forwarded payload
        self._last_index_key: dict[str, tuple[Any, Any]] = {}  # cumulative topics: (watermark key, its entry)
        self._z_last_ts: dict[str, datetime] = {}            # .z topics: last-forwarded entry payload ts

        self._counters: dict[str, dict[str, int]] = {}

    # -- introspection (architecture-plan.md §A.3.1 / §A.8) -----------------

    def set_reference(self, utc: datetime) -> None:
        """Explicitly set REFERENCE_UTC_TIMESTAMP.

        Normally unnecessary — per requirement-spec.md §9.2 the reference is
        simply "the first message to survive the universal gate," which this
        class already tracks on its own. Exposed for callers (or tests) that
        need to pin the origin explicitly. See DECISIONS.md #2.
        """
        self._reference_utc = utc

    def set_dedup_enabled(self, enabled: bool) -> None:
        """Enable/disable the continuous dedup rule (§9.1). See the
        `_dedup_enabled` docstring in `__init__` for why a caller with its
        own pre-forwarding buffering stage needs this."""
        self._dedup_enabled = enabled

    @property
    def counters(self) -> dict[str, dict[str, int]]:
        return self._counters

    @property
    def stream_lag_s(self) -> float:
        return self._stream_lag.total_seconds()

    @property
    def reference_utc(self) -> Optional[datetime]:
        return self._reference_utc

    # -- the pipeline ---------------------------------------------------------

    async def normalize(self, lines: AsyncIterator[RawLine]) -> AsyncIterator[NormalizedMessage]:
        async for line in lines:
            for msg in self.process_line(line):
                yield msg

    def process_line(self, line: RawLine) -> list[NormalizedMessage]:
        """Process one RawLine, synchronously, returning zero or more
        NormalizedMessage (zero for a synthetic/gated/deduped line, one for a
        non-.z line, zero-or-more for a .z line split into entries)."""
        topic = line.topic

        if topic in SYNTHETIC_TOPICS:
            return []

        if topic.endswith(".z"):
            return self._process_z(line)

        return self._process_generic(line)

    # -- per-class handling -----------------------------------------------

    def _extract_ec_payload_ts(self, data: Any) -> Optional[datetime]:
        if not isinstance(data, dict):
            return None
        raw = data.get("Utc")
        if not raw:
            return None
        return _parse_timestamp(raw)

    def _process_generic(self, line: RawLine) -> list[NormalizedMessage]:
        # Candidate STREAM_LAG for this line — only ExtrapolatedClock ever
        # changes it. Deliberately NOT committed to self._stream_lag until
        # this message is confirmed to survive the universal gate below.
        # requirement-spec.md §9.2 requires the gate to run "once per
        # message... before anything else in the normalizer"; committing
        # STREAM_LAG first (the pre-fix order) let a single gated-out zombie
        # ExtrapolatedClock message poison STREAM_LAG for every SUBSEQUENT
        # message in the session, even though the zombie message itself was
        # correctly dropped by the gate.
        candidate_stream_lag = self._stream_lag
        if line.topic == REFERENCE_TOPIC:
            payload_ts = self._extract_ec_payload_ts(line.data)
            if payload_ts is not None:
                candidate_stream_lag = line.envelope_ts - payload_ts

        utc_ts = line.envelope_ts - candidate_stream_lag
        gated = self._gate(line.topic, utc_ts)
        if gated is None:
            return []

        # Survived the gate — now safe to commit the candidate STREAM_LAG.
        self._stream_lag = candidate_stream_lag

        data = line.data
        if self._dedup_enabled:
            if line.topic in CUMULATIVE_LIST_TOPICS:
                data = self._dedup_cumulative(line.topic, data)
                if data is None:
                    self._bump(line.topic, "dedup_suppressed")
                    return []
            else:
                if self._dedup_singleton(line.topic, data):
                    self._bump(line.topic, "dedup_suppressed")
                    return []

        msg = self._stamp(line.topic, data, line.envelope_ts, utc_ts)
        self._bump(line.topic, "forwarded")
        return [msg]

    def _process_z(self, line: RawLine) -> list[NormalizedMessage]:
        if not isinstance(line.data, str):
            return []
        try:
            decompressed = decompress_z_data(line.data)
        except Exception:
            logger.debug(f"StreamNormalizer: failed to decompress {line.topic}")
            return []

        for payload_ts_str, entry_data in split_z_entries(line.topic, decompressed):
            utc_ts = _parse_timestamp(payload_ts_str) if payload_ts_str else None
            if utc_ts is None:
                utc_ts = line.envelope_ts  # fallback: entry carried no own timestamp

            gated = self._gate(line.topic, utc_ts)
            if gated is None:
                continue

            # Continuous dedup for .z (§9.1): entries are already split by
            # payload timestamp, which is a strictly-ordered key within a
            # topic — a resent burst's entries all carry timestamps <= the
            # newest one already forwarded, which is exactly the key/index
            # high-water-mark rule applied to a timestamp-keyed structure.
            # Gated by `_dedup_enabled` for the same reason as the generic
            # path — see its `set_dedup_enabled` docstring.
            #
            # Compared as PARSED datetimes (utc_ts), not the raw ISO string:
            # a naive string compare assumes every payload timestamp is
            # formatted with the same width/precision, which is not
            # guaranteed (e.g. a whole-second timestamp with no fractional
            # part sorts lexicographically AFTER one with a fractional part
            # — "...:01Z" > "...:01.250000Z" — even though it is earlier).
            # Found during AC-1 implementation: this silently suppressed
            # 3-of-4 Position.z entries per round whenever a round's first
            # entry landed exactly on a whole second. Real F1 payloads are
            # consistently formatted (architecture-plan.md/artifact 017 §5.1
            # confirms 4,542/4,542 checked .z entries), so this was latent
            # in production; comparing the already-parsed timestamp instead
            # of the raw string is strictly more correct and removes the
            # format assumption entirely, with no behavior change for
            # consistently-formatted input.
            if self._dedup_enabled and payload_ts_str is not None:
                last_ts = self._z_last_ts.get(line.topic)
                if last_ts is not None and utc_ts <= last_ts:
                    self._bump(line.topic, "dedup_suppressed")
                    continue
                self._z_last_ts[line.topic] = utc_ts

            # AC-1/AC-2: hold in the shared reorder buffer instead of
            # stamping/emitting immediately — `_stamp()` (and the counter
            # bump for "actually forwarded downstream") happens at RELEASE
            # time, in `_release_ready`/`flush`/`poll_wall_clock_backstop`,
            # not here.
            self._insert_z(utc_ts, line.topic, entry_data, line.envelope_ts)

        return self._release_ready()

    # -- AC-1/AC-2/AC-3 reorder buffer ---------------------------------------

    def _insert_z(self, utc_ts: datetime, topic: str, entry_data: Any,
                   envelope_ts: datetime) -> None:
        """Insert one gate/dedup-checked CarData.z/Position.z entry into the
        shared reorder buffer, keyed on its OWN payload timestamp (never the
        envelope clock, never STREAM_LAG-adjusted — AC-1's key). Advances the
        release watermark — AC-2's hard constraint: this is the ONLY place
        the watermark is ever written, and it is only ever reached from a
        CarData.z/Position.z entry."""
        arrival_wall = self._now()
        entry = (utc_ts, topic, entry_data, envelope_ts, arrival_wall)
        bisect.insort(self._z_buffer, entry, key=lambda e: e[0])
        if self._z_watermark is None or utc_ts > self._z_watermark:
            self._z_watermark = utc_ts

    def _release_ready(self) -> list[NormalizedMessage]:
        """Release (in ascending payload-timestamp order) every buffered
        entry whose own timestamp is now more than `Z_REORDER_WINDOW` behind
        the release watermark — the primary, content-driven release path."""
        if self._z_watermark is None:
            return []
        cutoff = self._z_watermark - Z_REORDER_WINDOW
        released: list[NormalizedMessage] = []
        while self._z_buffer and self._z_buffer[0][0] <= cutoff:
            utc_ts, topic, entry_data, envelope_ts, _arrival = self._z_buffer.pop(0)
            released.append(self._stamp(topic, entry_data, envelope_ts, utc_ts))
            self._bump(topic, "forwarded")
        return released

    def flush(self) -> list[NormalizedMessage]:
        """AC-3's end-of-stream flush (file-impact-map.md §1's correction:
        hooked to exhaustion of the async iterator in preprocessor.py, NEVER
        the `_SessionEnd` marker, which is swallowed in file_reader.py and
        inert during live tail-follow). Releases every entry still held in
        the reorder buffer, in ascending timestamp order, stamped exactly as
        `_release_ready` stamps a normally-released entry. Still
        content-driven (the trigger is "no more input exists", never a
        timer) — see the class docstring's determinism note."""
        released = [
            self._stamp(topic, entry_data, envelope_ts, utc_ts)
            for utc_ts, topic, entry_data, envelope_ts, _arrival in self._z_buffer
        ]
        for _utc_ts, topic, *_rest in self._z_buffer:
            self._bump(topic, "forwarded")
        self._z_buffer = []
        return released

    def poll_wall_clock_backstop(self) -> list[NormalizedMessage]:
        """AC-3's wall-clock backstop: release any buffered entry that has
        sat in the buffer for `wall_clock_backstop_s` of REAL (wall-clock)
        time, independent of any message-driven watermark advance. This is
        the ONE non-content-driven release path in this class — deliberately
        carved out of the determinism guarantee (rules/data-processing.md,
        requirement-spec.md §4) because it only ever fires when there is
        genuinely no content left to drive a decision (whole-feed silence,
        up to 80.5-87.3s observed in real sessions — requirement-spec.md
        AC-3). Not invoked automatically; a caller (preprocessor.py's
        periodic backstop task) must poll it. No-op if the backstop is
        disabled (`wall_clock_backstop_s=None`)."""
        if self._wall_clock_backstop_s is None or not self._z_buffer:
            return []
        now = self._now()
        released: list[NormalizedMessage] = []
        remaining: list[tuple[datetime, str, Any, datetime, float]] = []
        for entry in self._z_buffer:
            utc_ts, topic, entry_data, envelope_ts, arrival_wall = entry
            if now - arrival_wall >= self._wall_clock_backstop_s:
                released.append(self._stamp(topic, entry_data, envelope_ts, utc_ts))
                self._bump(topic, "forwarded")
            else:
                remaining.append(entry)
        self._z_buffer = remaining
        return released

    # -- gate / reference ---------------------------------------------------

    def _gate(self, topic: str, utc_ts: datetime) -> Optional[bool]:
        """Universal 60-minute-before-scheduled-start gate (§9.2). Returns
        None (drop) or True (keep). No-op (always keep) if no scheduled start
        was provided — see DECISIONS.md #3."""
        if self._gate_cutoff is None:
            return True
        if utc_ts < self._gate_cutoff:
            self._bump(topic, "gate_dropped")
            return None
        return True

    def _stamp(self, topic: str, data: Any, envelope_ts: datetime, utc_ts: datetime) -> NormalizedMessage:
        if self._reference_utc is None:
            self._reference_utc = utc_ts
        offset_ms = int((utc_ts - self._reference_utc).total_seconds() * 1000)
        return NormalizedMessage(
            topic=topic, data=data, envelope_ts=envelope_ts,
            utc_timestamp=utc_ts, offset_ms=offset_ms,
        )

    # -- continuous dedup rule (§9.1) ----------------------------------------

    def _dedup_singleton(self, topic: str, data: Any) -> bool:
        """True if `data` is byte-identical to the last payload forwarded for
        this topic (skip); records it as "last" and returns False otherwise."""
        json_str = json.dumps(data, default=str, sort_keys=True)
        if self._last_emitted_json.get(topic) == json_str:
            return True
        self._last_emitted_json[topic] = json_str
        return False

    def _dedup_cumulative(self, topic: str, data: Any) -> Optional[Any]:
        """Filter a cumulative-list topic's resent array by the key/index of
        the most-recently-emitted entry (requirement-spec.md §9.1's
        correction to architecture-plan.md §A.3.3's stale Utc-comparison
        text): keep only entries with a higher index/key than the watermark.

        Returns the rebuilt (filtered) payload, or None if nothing survives.
        """
        keyed = _entry_index_keys(topic, data)
        if keyed is None:
            # Not an indexed structure this rule recognizes — treat as a
            # singleton (content dedup only). See DECISIONS.md #4.
            return None if self._dedup_singleton(topic, data) else data

        watermark, watermark_entry = self._last_index_key.get(topic, (None, None))

        # Invariant check (§9.1): if the watermark entry re-appears in this
        # burst, it must be byte-identical to what we recorded when we last
        # emitted it. Expected to hold always — log loudly, don't crash, if
        # it ever doesn't (accepted-risk philosophy consistent with D5).
        if watermark is not None:
            resent_watermark_entry = keyed.get(str(watermark))
            if resent_watermark_entry is not None and resent_watermark_entry != watermark_entry:
                logger.warning(
                    f"StreamNormalizer: {topic} watermark entry {watermark!r} changed on "
                    f"resend — expected byte-identical, got a different payload. "
                    f"Proceeding with the high-water-mark filter anyway."
                )

        kept: dict[str, Any] = {}
        new_watermark = watermark
        new_watermark_entry = watermark_entry
        for key, entry in sorted(keyed.items(), key=lambda kv: _sort_key(kv[0])):
            if watermark is not None and _sort_key(key) <= _sort_key(str(watermark)):
                continue
            kept[key] = entry
            if new_watermark is None or _sort_key(key) > _sort_key(str(new_watermark)):
                new_watermark = key
                new_watermark_entry = entry

        if new_watermark is not None:
            self._last_index_key[topic] = (new_watermark, new_watermark_entry)

        if not kept:
            return None
        return _rebuild_cumulative_payload(topic, data, kept)

    # -- counters -------------------------------------------------------------

    def _bump(self, topic: str, key: str) -> None:
        counters = self._counters.setdefault(
            topic, {"forwarded": 0, "dedup_suppressed": 0, "gate_dropped": 0}
        )
        counters[key] = counters.get(key, 0) + 1
