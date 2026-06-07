"""
Background scanner for pre-processing session data.

Runs the FileReader in fast mode to build snapshots, detect events, and
populate the telemetry database ahead of playback. Reports progress to
the SessionEngine which forwards it to clients.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.processing.file_reader import read_jsonl, RawMessage
from app.processing.database import SessionDatabase
from app.processing.message_bus import SessionMessageBus

logger = logging.getLogger(__name__)

# Minimum messages between snapshots to avoid wasting storage during quiet periods
SNAPSHOT_MIN_MESSAGES = 100


class BackgroundScanner:
    """Pre-processes a session's JSONL file in the background."""

    def __init__(
        self,
        session_path: Path,
        db: SessionDatabase,
        start_time: datetime,
        on_progress: Optional[Callable[[float], None]] = None,
    ):
        self._session_path = session_path
        self._db = db
        self._start_time = start_time
        self._on_progress = on_progress

        self._bus = SessionMessageBus()
        self._task: Optional[asyncio.Task] = None
        self._complete = False
        self._progress = 0.0
        self._message_count = 0
        self._messages_since_snapshot = 0

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def progress(self) -> float:
        return self._progress

    def start(self) -> None:
        """Start background scanning."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop background scanning."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Main scanning loop — reads at max speed."""
        try:
            logger.info(f"Scanner starting for {self._session_path.name}")

            # Count total lines for progress estimation
            live_file = self._session_path / "live.jsonl"
            total_lines = sum(1 for _ in open(live_file, "r", encoding="utf-8"))
            lines_processed = 0

            async for msg in read_jsonl(self._session_path, fast=True):
                self._message_count += 1
                self._messages_since_snapshot += 1
                lines_processed += 1

                # Emit to internal bus (processors would subscribe here)
                self._bus.emit(msg.topic, msg.data, msg.timestamp)

                # Take snapshots when enough messages have accumulated
                offset_ms = int((msg.timestamp - self._start_time).total_seconds() * 1000)
                if self._messages_since_snapshot >= SNAPSHOT_MIN_MESSAGES:
                    self._take_snapshot(offset_ms)
                    self._messages_since_snapshot = 0

                # Report progress
                if total_lines > 0 and lines_processed % 1000 == 0:
                    self._progress = min(99.0, (lines_processed / total_lines) * 100)
                    if self._on_progress:
                        self._on_progress(self._progress)

                # Yield periodically to avoid blocking
                if self._message_count % 2000 == 0:
                    await asyncio.sleep(0)

            self._complete = True
            self._progress = 100.0
            if self._on_progress:
                self._on_progress(100.0)

            logger.info(
                f"Scanner complete: {self._message_count} messages, "
                f"{len(self._db.list_snapshots())} snapshots"
            )

        except asyncio.CancelledError:
            logger.info("Scanner cancelled")
            raise
        except Exception:
            logger.exception("Scanner error")

    def _take_snapshot(self, offset_ms: int) -> None:
        """Take a snapshot of all processor states and save to database.

        For Phase 1, this saves a minimal snapshot. Processors will be added
        in later phases and will contribute their state via the message bus.
        """
        # Placeholder: in later phases, processors register snapshot callbacks
        snapshot_state: dict[str, Any] = {
            "offset_ms": offset_ms,
            "message_count": self._message_count,
        }
        self._db.save_snapshot(offset_ms, snapshot_state)
