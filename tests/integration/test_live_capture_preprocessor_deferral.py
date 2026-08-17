"""Live-capture path coverage for `SessionPreProcessor` construction timing
(2026-08-17-047 WB-1 resume, file-impact-map.md §1.3).

Before this task, `live_capture.py`'s `_capture_loop` had **no test coverage
at all** (file-impact-map.md §1.3: "file-impact-map found there was no
existing test coverage of `live_capture.py`'s `SessionPreProcessor`
construction timing"). This file covers the two behaviours this task's
implementation introduced:

  1. Normal path: `SessionPreProcessor` construction is deferred from "the
     moment `live.jsonl` exists" to "the moment the `SessionInfo` topic is
     specifically seen" -- avoiding the `subscribe.json` write-order race
     (file-impact-map.md §1.3) -- and the REAL `scheduled_start_utc` parsed
     from that SessionInfo's own in-memory `data` reaches the constructor.
  2. Fallback path: if `SessionInfo` never arrives at all (pathological --
     not expected in production) but data WAS captured, the `finally` block
     still builds the DB, with `scheduled_start_utc=None` (StreamNormalizer's
     documented no-op-gate default, DECISIONS.md #3) rather than silently
     losing the capture's processed DB.

Drives `LiveCaptureService._capture_loop` directly against a fake
`F1SignalRClient` (a real SignalR connection, HLS audio, and ffmpeg are all
out of scope for what this test covers -- construction TIMING and the value
passed to the constructor) and a spy in place of `SessionPreProcessor` that
records every construction call without doing any real file I/O/processing.
"""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.services import live_capture as live_capture_module
from app.services.live_capture import LiveCaptureService, CaptureStatus


class _FakeSignalRClient:
    """Stands in for `F1SignalRClient`: hands back a caller-fillable queue
    instead of a real SignalR connection/thread."""

    def __init__(self, cache_path, timeout):
        self.cache_path = cache_path
        self.timeout = timeout
        self.message_count = 0
        self.is_alive = True
        self.queue: asyncio.Queue = asyncio.Queue()

    def start(self, loop):
        return self.queue

    def stop(self):
        pass


class _SpyPreProcessor:
    """Stands in for `SessionPreProcessor`: records constructor calls,
    does no real file I/O or message processing."""

    calls: list = []

    def __init__(self, session_path, session_type, *, scheduled_start_utc=None):
        self.session_path = session_path
        self.session_type = session_type
        self.scheduled_start_utc = scheduled_start_utc
        # Created in __init__, not run() -- `asyncio.create_task(run())`
        # only SCHEDULES the coroutine, it doesn't start executing it
        # immediately, so a stop() call that races ahead of run()'s first
        # await must not silently no-op against an event that doesn't exist
        # yet.
        self._stop_event = asyncio.Event()
        type(self).calls.append(
            {"session_path": session_path, "session_type": session_type,
             "scheduled_start_utc": scheduled_start_utc})

    async def run(self, tail_follow=False, **kwargs):
        # Runs until stop() is awaited -- mirrors the real tail-follow
        # coroutine's lifetime without doing any real work.
        await self._stop_event.wait()

    async def stop(self):
        self._stop_event.set()

    def close(self):
        pass


class _Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.cache_path = self.root / "2026" / "01_Test_GP" / "9999_Race"
        self.cache_path.mkdir(parents=True)
        _SpyPreProcessor.calls = []

    def _make_service(self) -> LiveCaptureService:
        service = LiveCaptureService(cache_dir=str(self.root))
        # Audio/ffmpeg is out of scope for this file -- no-op it so the test
        # never spawns a real process or waits on real wall-clock scheduling.
        service._start_audio = lambda *a, **k: None
        return service

    async def _run_capture_to_completion(self, service, queue: asyncio.Queue,
                                          session_id: str = "test-session"):
        service._captures[session_id] = {
            "status": CaptureStatus.CONNECTING,
            "cache_path": self.cache_path,
            "message_count": 0,
            "error": None,
            "session_type": "race",
        }
        with mock.patch.object(live_capture_module, "F1SignalRClient",
                                lambda cache_path, timeout: _FakeClientWithQueue(queue)), \
             mock.patch.object(live_capture_module, "SessionPreProcessor", _SpyPreProcessor):
            await asyncio.wait_for(
                service._capture_loop(session_id, self.cache_path), timeout=5.0)


class _FakeClientWithQueue(_FakeSignalRClient):
    """`_FakeSignalRClient` variant constructed from a pre-built queue
    (rather than creating its own) so the test can populate the queue
    before the client even exists."""

    def __init__(self, queue: asyncio.Queue):
        self.message_count = 0
        self.is_alive = True
        self.queue = queue


def _session_info_message(start_date: str, gmt_offset: str) -> dict:
    return {
        "type": "timing",
        "topic": "SessionInfo",
        "data": {"StartDate": start_date, "GmtOffset": gmt_offset, "Path": "2026/foo/"},
    }


_DISCONNECT = {"type": "status", "status": "disconnected"}


class NormalPathDeferredConstruction(_Harness):
    async def test_construction_deferred_until_sessioninfo_and_real_schedule_reaches_constructor(self):
        # live.jsonl must already exist (as the real SignalR client would
        # have written it) for the deferred-construction branch to fire.
        (self.cache_path / "live.jsonl").write_text(
            json.dumps({"Type": "SessionInfo", "DateTime": "2026-07-18T09:00:00.000Z",
                        "Json": {}}) + "\n")

        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(_session_info_message("2026-07-18T10:00:00", "+01:00:00"))
        queue.put_nowait(_DISCONNECT)

        service = self._make_service()
        await self._run_capture_to_completion(service, queue)

        self.assertEqual(len(_SpyPreProcessor.calls), 1,
                          "SessionPreProcessor must be constructed exactly once")
        call = _SpyPreProcessor.calls[0]
        self.assertEqual(call["session_path"], self.cache_path)
        # StartDate=10:00:00 local, GmtOffset=+01:00:00 -> 09:00:00 UTC.
        expected = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(call["scheduled_start_utc"], expected,
                          "the REAL scheduled_start_utc parsed from SessionInfo's own "
                          "in-memory data must reach the constructor, not None")

    async def test_future_scheduled_start_schedules_a_delayed_audio_task(self):
        """Branch coverage for `_schedule_audio_start`'s "start is still in
        the future" path (as opposed to the "already passed" path the sibling
        test above exercises via a fixed past date) -- confirms a delayed
        audio-start task gets scheduled rather than starting immediately."""
        (self.cache_path / "live.jsonl").write_text(
            json.dumps({"Type": "SessionInfo", "DateTime": "2026-07-18T09:00:00.000Z",
                        "Json": {}}) + "\n")

        future_local = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(_session_info_message(future_local.strftime("%Y-%m-%dT%H:%M:%S"), "+00:00:00"))
        queue.put_nowait(_DISCONNECT)

        service = self._make_service()
        await self._run_capture_to_completion(service, queue)

        # The task was created (and cancelled cleanly by the capture loop's
        # own finally-block teardown) -- proving the "still in the future"
        # branch, not the "already passed, start now" branch, was taken.
        self.assertTrue(service._audio_scheduled)
        self.assertIsNotNone(service._audio_start_task)

    async def test_no_construction_before_sessioninfo_arrives_even_with_live_jsonl_present(self):
        (self.cache_path / "live.jsonl").write_text(
            json.dumps({"Type": "Heartbeat", "DateTime": "2026-07-18T09:00:00.000Z",
                        "Json": {}}) + "\n")

        queue: asyncio.Queue = asyncio.Queue()
        # A non-SessionInfo "timing" message must NOT trigger construction,
        # even though live.jsonl already exists -- construction is deferred
        # specifically to the SessionInfo topic (file-impact-map.md §1.3),
        # not merely to "live.jsonl exists" (the old, race-prone condition).
        queue.put_nowait({"type": "timing", "topic": "Heartbeat", "data": {}})
        seen_before_sessioninfo = len(_SpyPreProcessor.calls)

        service = self._make_service()

        # Drive just the one Heartbeat message through, then inspect state
        # before ending the capture (so the fallback/finally-block path
        # doesn't also run and confuse what this test is checking).
        service._captures["s"] = {
            "status": CaptureStatus.CONNECTING, "cache_path": self.cache_path,
            "message_count": 0, "error": None, "session_type": "race",
        }
        with mock.patch.object(live_capture_module, "F1SignalRClient",
                                lambda cache_path, timeout: _FakeClientWithQueue(queue)), \
             mock.patch.object(live_capture_module, "SessionPreProcessor", _SpyPreProcessor):
            queue.put_nowait(_DISCONNECT)
            await asyncio.wait_for(service._capture_loop("s", self.cache_path), timeout=5.0)

        # Exactly one construction happened -- via the FALLBACK path (no
        # SessionInfo ever arrived), not the normal SessionInfo-triggered
        # path. Covered in detail by the fallback test class below; this
        # test only confirms the normal-path branch specifically did NOT
        # fire for a non-SessionInfo message.
        self.assertEqual(len(_SpyPreProcessor.calls), seen_before_sessioninfo + 1)
        self.assertIsNone(_SpyPreProcessor.calls[-1]["scheduled_start_utc"])


class FallbackPathSessionInfoNeverArrives(_Harness):
    async def test_db_still_built_with_no_schedule_if_sessioninfo_never_arrives(self):
        """Pathological case (file-impact-map.md §1.3 / implementation-plan.md's
        Design Choices): data was captured to live.jsonl but the SessionInfo
        topic never arrived at all during the capture, so the normal
        construction branch never fires. The `finally` block's safety net
        must still build the DB -- with `scheduled_start_utc=None` (the
        documented no-op-gate default) -- rather than silently losing the
        capture's processed DB."""
        (self.cache_path / "live.jsonl").write_text(
            json.dumps({"Type": "Heartbeat", "DateTime": "2026-07-18T09:00:00.000Z",
                        "Json": {}}) + "\n")

        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait({"type": "timing", "topic": "Heartbeat", "data": {}})
        queue.put_nowait(_DISCONNECT)

        service = self._make_service()
        await self._run_capture_to_completion(service, queue)

        self.assertEqual(len(_SpyPreProcessor.calls), 1)
        call = _SpyPreProcessor.calls[0]
        self.assertEqual(call["session_path"], self.cache_path)
        self.assertIsNone(
            call["scheduled_start_utc"],
            "the fallback construction site passes no scheduled_start_utc at "
            "all (positional call, `SessionPreProcessor(cache_path, \"\")`) "
            "-- must default to None, the documented no-op-gate value",
        )

    async def test_no_db_built_at_all_if_live_jsonl_was_never_written(self):
        """If NOTHING was ever captured to disk (no live.jsonl at all), the
        fallback safety net must not construct a DB either -- there would be
        nothing for it to process."""
        # No live.jsonl written at all.
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait(_DISCONNECT)

        service = self._make_service()
        await self._run_capture_to_completion(service, queue)

        self.assertEqual(len(_SpyPreProcessor.calls), 0)


if __name__ == "__main__":
    unittest.main()
