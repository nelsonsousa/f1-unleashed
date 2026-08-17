"""
JSONL File Reader — pure tail-follow forwarder.

Reads F1 timing data lines from JSONL files and forwards them, in strict file
order, with no reordering, no buffering window, and no `.z` handling. Per the
2026-07-27-003 pipeline redesign (`docs/artifacts/2026-07-27-003-pipeline-redesign/`),
those responsibilities (STREAM_LAG timestamp correction, `.z` decompression/
splitting, and the continuous dedup rule) now live in
`app/processing/stream_normalizer.py`. This module's only job is: bytes -> lines
-> RawLine, in the order they appear in the file, plus tail-follow polling and
live-sim pacing.

Two modes:
- Normal: yields RawLine(topic, data, envelope_ts) at read speed
- Tail-follow: when reaching EOF without _SessionEnd marker, polls for new content
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)

# End-of-session marker written by SignalR client
SESSION_END_MARKER = "_SessionEnd"


@dataclass
class RawLine:
    """One forwarded line from the JSONL file, in file order.

    `data` is exactly what `Json` deserialized to — for `.z` topics this is
    still the base64-encoded compressed string; decompression is the
    normalizer's job, not the reader's (AC-3: the reader has no logic beyond
    sequential reading).
    """
    topic: str
    data: Any
    envelope_ts: datetime


def _parse_timestamp(dt_str: str) -> Optional[datetime]:
    """Parse a datetime string, handling various F1 formats.

    Always returns timezone-aware (UTC) datetimes to avoid naive/aware
    comparison errors downstream.
    """
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def parse_scheduled_start_utc(session_info: dict) -> Optional[datetime]:
    """Tz-aware scheduled session start (UTC), from an F1 SessionInfo dict's
    raw `StartDate` (local) + `GmtOffset` fields (e.g.
    StartDate=2026-07-04T16:00:00, GmtOffset=01:00:00 -> 15:00 UTC).

    Single shared implementation (2026-08-17-047 WB-1 resume,
    file-impact-map.md §1.5) for the `StartDate + GmtOffset -> UTC`
    computation needed at every `SessionPreProcessor` construction site that
    feeds `StreamNormalizer(scheduled_start_utc=...)` — replay
    (`session.py`), CDN download (`livetiming_fetcher.py`), and live capture
    (`live_capture.py`). Always tz-aware: `StreamNormalizer._gate()` compares
    against `_parse_timestamp()`'s output, which is always tz-aware, so a
    naive result here would raise `TypeError` on the first comparison — this
    replaces `live_capture.py`'s previous naive-`datetime`-returning helper
    of the same computation.

    Returns None if `StartDate`/`GmtOffset` are missing or unparseable — the
    caller passes None through, which is `StreamNormalizer`'s documented
    no-op-gate default (DECISIONS.md #3).
    """
    if not isinstance(session_info, dict):
        return None
    sd = session_info.get("StartDate")
    off = session_info.get("GmtOffset")
    if not sd or not off:
        return None
    try:
        start_local = datetime.fromisoformat(sd).replace(tzinfo=None)
        off = off.strip()
        sign = -1 if off.startswith("-") else 1
        h, m, s = (off.lstrip("+-").split(":") + ["0", "0", "0"])[:3]
        offset = sign * timedelta(hours=int(h), minutes=int(m), seconds=int(float(s)))
        return (start_local - offset).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def read_jsonl(
    session_path: Path,
    *,
    fast: bool = False,
    tail_follow: bool = False,
    poll_interval: float = 0.5,
    on_caught_up: Optional[Callable] = None,
    stop_follow: Optional[asyncio.Event] = None,
    pace: bool = False,
    speed: float = 1.0,
    _now: Callable[[], float] = time.monotonic,
    _sleep: Callable = asyncio.sleep,
) -> AsyncIterator[RawLine]:
    """Read a session's JSONL file and forward each line, in file order.

    Args:
        session_path: Directory containing live.jsonl
        fast: If True, read at max speed (for background scanning)
        tail_follow: If True, poll for new content at EOF instead of stopping
        poll_interval: Seconds between tail polls (only when tail_follow=True)
        stop_follow: If set while tail-following, stop at the next EOF so the
            consumer can finalize (used when a live capture ends).
        pace: Live-SIMULATION mode — sleep between lines so each is released at
            its envelope (arrival) timestamp relative to the first, reproducing
            the real arrival cadence. Paces the READ (input).
        speed: wall-time multiplier for `pace` (2.0 = 2x real speed).
        _now, _sleep: injectable monotonic clock + async sleep (deterministic tests).

    Yields:
        RawLine(topic, data, envelope_ts) in strict file order — no reordering.
    """
    live_file = session_path / "live.jsonl"
    if not live_file.exists():
        raise FileNotFoundError(f"No live.jsonl found at {session_path}")

    session_ended = False
    yield_count = 0
    caught_up = False

    # Live-sim pacing: hold each line until its envelope timestamp (relative to
    # the first) elapses in (scaled) wall time. Injectable clock/sleep for tests.
    _pace_start_wall = None
    _pace_start_ts = None

    async def _pace(ts):
        nonlocal _pace_start_wall, _pace_start_ts
        if _pace_start_ts is None:
            _pace_start_ts = ts
            _pace_start_wall = _now()
            return
        delay = (ts - _pace_start_ts).total_seconds() / speed - (_now() - _pace_start_wall)
        if delay > 0:
            await _sleep(delay)

    with open(live_file, "r", encoding="utf-8") as f:
        while True:
            pos = f.tell()
            line = f.readline()

            # A tail-followed line without a trailing newline is a partial write — the
            # appender hasn't flushed the '\n' yet. Rewind and treat it as EOF so the
            # whole line is re-read once complete; never split/drop a torn JSON line (H7).
            # (A complete-file scan, tail_follow=False, keeps a final newline-less line.)
            if line and tail_follow and not line.endswith("\n"):
                f.seek(pos)
                line = ""

            if not line:
                # EOF reached (or a rewound partial line)
                if session_ended or not tail_follow:
                    break
                # Signal that initial file content has been consumed
                if not caught_up:
                    caught_up = True
                    if on_caught_up:
                        on_caught_up()
                # Graceful stop: the data source signalled that no more
                # content is coming — stop following so the consumer can
                # finalize. All written lines have been read at this EOF.
                if stop_follow is not None and stop_follow.is_set():
                    break
                # Tail-follow: wait for more content
                await asyncio.sleep(poll_interval)
                continue

            line = line.strip()
            if not line:
                continue

            try:
                msg_data = json.loads(line)
            except json.JSONDecodeError:
                continue

            topic = msg_data.get("Type", "")
            datetime_str = msg_data.get("DateTime", "")
            data = msg_data.get("Json")

            # Check for end marker (skip in tail-follow mode — reconnections
            # produce multiple markers mid-file)
            if topic == SESSION_END_MARKER:
                if not tail_follow:
                    session_ended = True
                continue

            envelope_ts = _parse_timestamp(datetime_str)
            if not envelope_ts:
                continue

            if pace:
                await _pace(envelope_ts)

            yield RawLine(topic=topic, data=data, envelope_ts=envelope_ts)
            yield_count += 1

            # Yield to event loop periodically in normal mode
            if not fast and yield_count % 500 == 0:
                await asyncio.sleep(0)

    logger.info(f"FileReader finished: {yield_count} lines from {session_path.name}")


def load_subscribe_json(session_path: Path) -> dict[str, Any]:
    """Load the initial state snapshot (subscribe.json)."""
    subscribe_file = session_path / "subscribe.json"
    if subscribe_file.exists():
        try:
            with open(subscribe_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"Failed to parse subscribe.json in {session_path.name}: {e}")
    return {}
