"""
JSONL File Reader with tail-follow support.

Reads F1 timing data from JSONL files, decompresses .z topics, splits
CarData.z/Position.z into individual entries by payload timestamp, and
reorders messages within a 1-second window.

Two modes:
- Normal: yields messages as (topic, data, timestamp) tuples at read speed
- Tail-follow: when reaching EOF without _SessionEnd marker, polls for new content
"""

import asyncio
import json
import logging
import zlib
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)

# End-of-session marker written by SignalR client
SESSION_END_MARKER = "_SessionEnd"


@dataclass
class RawMessage:
    """A raw timing message from the JSONL file."""
    topic: str
    data: Any
    timestamp: datetime


def decompress_z_data(data: str) -> Any:
    """Decompress .z topic data (base64 → zlib raw inflate → JSON)."""
    decoded = base64.b64decode(data)
    decompressed = zlib.decompress(decoded, -zlib.MAX_WBITS)
    return json.loads(decompressed)


def split_z_entries(topic: str, data: Any) -> list[tuple[Optional[str], Any]]:
    """Split a decompressed .z message into individual entries with payload timestamps.

    Returns list of (iso_timestamp_or_None, single_entry_data) pairs.
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


def _parse_timestamp(dt_str: str) -> Optional[datetime]:
    """Parse a datetime string, handling various F1 formats.

    Always returns timezone-aware (UTC) datetimes to avoid naive/aware
    comparison errors in the reorder buffer.
    """
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


async def read_jsonl(
    session_path: Path,
    *,
    fast: bool = False,
    tail_follow: bool = False,
    poll_interval: float = 0.5,
    on_caught_up: Optional[Callable] = None,
    stop_follow: Optional[asyncio.Event] = None,
) -> AsyncIterator[RawMessage]:
    """Read and process messages from a session's JSONL file.

    Args:
        session_path: Directory containing live.jsonl
        fast: If True, read at max speed (for background scanning)
        tail_follow: If True, poll for new content at EOF instead of stopping
        poll_interval: Seconds between tail polls (only when tail_follow=True)
        stop_follow: If set while tail-following, stop at the next EOF so the
            consumer can finalize (used when a live capture ends).

    Yields:
        RawMessage tuples in chronological order (reordered within 1s window)
    """
    live_file = session_path / "live.jsonl"
    if not live_file.exists():
        raise FileNotFoundError(f"No live.jsonl found at {session_path}")

    reorder_window = timedelta(seconds=1.0)
    reorder_buffer: list[tuple[datetime, RawMessage]] = []
    newest_ts: Optional[datetime] = None
    session_ended = False
    yield_count = 0
    caught_up = False

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
                    # Flush reorder buffer before signalling
                    if reorder_buffer:
                        reorder_buffer.sort(key=lambda x: x[0])
                        for _, msg in reorder_buffer:
                            yield msg
                            yield_count += 1
                        reorder_buffer = []
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

            # Decompress .z topics
            if topic.endswith('.z') and isinstance(data, str):
                try:
                    data = decompress_z_data(data)
                except Exception:
                    logger.debug(f"Failed to decompress {topic}")
                    continue

            # Split .z topics into individual entries with payload timestamps
            if topic.endswith('.z') and isinstance(data, dict):
                entries = split_z_entries(topic, data)
                for payload_ts_str, entry_data in entries:
                    if payload_ts_str:
                        ts = _parse_timestamp(payload_ts_str) or envelope_ts
                    else:
                        ts = envelope_ts

                    reorder_buffer.append((ts, RawMessage(
                        topic=topic,
                        data=entry_data,
                        timestamp=ts,
                    )))
                    if newest_ts is None or ts > newest_ts:
                        newest_ts = ts
            else:
                # Non-.z topic: use envelope timestamp
                reorder_buffer.append((envelope_ts, RawMessage(
                    topic=topic,
                    data=data,
                    timestamp=envelope_ts,
                )))

            # Flush entries older than the reorder window
            if newest_ts and reorder_buffer:
                cutoff = newest_ts - reorder_window
                ready = []
                remaining = []
                for item in reorder_buffer:
                    if item[0] <= cutoff:
                        ready.append(item)
                    else:
                        remaining.append(item)

                if ready:
                    ready.sort(key=lambda x: x[0])
                    reorder_buffer = remaining
                    for _, msg in ready:
                        yield msg
                        yield_count += 1

            # Yield to event loop periodically in normal mode
            if not fast and yield_count % 500 == 0:
                await asyncio.sleep(0)

    # Flush remaining buffer entries
    if reorder_buffer:
        reorder_buffer.sort(key=lambda x: x[0])
        for _, msg in reorder_buffer:
            yield msg
            yield_count += 1

    logger.info(f"FileReader finished: {yield_count} messages from {session_path.name}")


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
