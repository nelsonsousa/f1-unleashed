"""AC-3 (requirement-spec.md; file-impact-map.md §1's correction) — coverage
for `preprocessor.py`'s actual implementation of the end-of-stream flush and
wall-clock backstop wiring.

`tests/integration/test_preprocessor_end_of_stream_flush.py` (test-plan.md's
named target for this wiring) has a mock-setup defect independent of this
implementation: it patches `StreamNormalizer.flush` as a class attribute via
`mock.patch.object(..., side_effect=_flush_side_effect, autospec=False)`,
whose `side_effect` signature expects a `self_norm` positional argument —
but a non-autospec'd Mock assigned as a class attribute is never bound via
the descriptor protocol, so `instance.flush()` calls the Mock with ZERO
arguments regardless of what the real implementation does, raising
`TypeError: _flush_side_effect() missing 1 required positional argument:
'self_norm'` the moment `preprocessor.py` actually calls `flush()` (see
implementation-plan.md for the confirmed reproduction against Python's own
`unittest.mock`). That test cannot pass against ANY implementation that
calls `self._normalizer.flush()` the normal way; it is reported as a test
defect, not fixed here (rules/testing.md: report, don't silently edit).

This file exercises the same wiring contract with correctly-constructed
mocks/wrappers (instance-level flush wrapping, proper class-level method
patching via a real function so Python's descriptor protocol binds `self`
normally), plus direct coverage of `_emit_flushed_message` and
`_wall_clock_backstop_loop`, which the above test would have exercised only
incidentally.
"""
import asyncio
import base64
import json
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor
from app.processing.stream_normalizer import NormalizedMessage


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


def _car_data_z(ts: str, num: str = "1", speed: int = 200) -> str:
    return _compress({"Entries": [{"Utc": ts, "Cars": {num: {"Channels": {"2": speed}}}}]})


class _Harness(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Practice"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

    def _write_lines(self, lines: list[str]) -> None:
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    def _make(self) -> SessionPreProcessor:
        with mock.patch("app.processing.database.transient_db_path",
                         return_value=self.root / "session.db"):
            return SessionPreProcessor(self.sess, "Practice")


class FlushRunsBeforeFinalizeSession(_Harness):
    """The wiring contract test-plan.md's named target test intended (see
    module docstring for why that file's own mock is broken)."""

    def setUp(self):
        super().setUp()
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        lines = [
            json.dumps({"Type": "SessionInfo", "DateTime": _iso(base), "Json": si}),
            json.dumps({"Type": "Heartbeat", "DateTime": _iso(base + timedelta(seconds=1)), "Json": {}}),
        ]
        self._write_lines(lines)

    async def test_flush_is_invoked_before_finalize_session(self):
        call_order = []
        p = self._make()
        try:
            orig_flush = p._normalizer.flush

            def _wrapped_flush():
                call_order.append("flush")
                return orig_flush()

            p._normalizer.flush = _wrapped_flush

            orig_finalize = TelemetryProcessor.finalize_session

            def _wrapped_finalize(self_tp, clock_time):
                call_order.append("finalize_session")
                return orig_finalize(self_tp, clock_time)

            with mock.patch.object(TelemetryProcessor, "finalize_session", _wrapped_finalize):
                await p.run()

            self.assertIn("flush", call_order)
            self.assertIn("finalize_session", call_order)
            self.assertLess(
                call_order.index("flush"), call_order.index("finalize_session"),
                "flush() must run BEFORE finalize_session — finalize_session "
                "closes the open lap using whatever is in drv.samples at "
                "that moment",
            )
        finally:
            p._db.close()

    async def test_flush_releases_a_buffered_z_entry_no_watermark_ever_advanced_it(self):
        """A single CarData.z entry, nothing else -- under AC-1's buffer it
        is held (nothing ever advances the watermark past it), so without
        the flush it would vanish silently at EOF exactly as
        file-impact-map.md AC-3 describes."""
        base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        self._write_lines([
            json.dumps({"Type": "SessionInfo", "DateTime": _iso(base), "Json": si}),
            json.dumps({"Type": "CarData.z", "DateTime": _iso(base + timedelta(seconds=1)),
                        "Json": _car_data_z(_iso(base + timedelta(milliseconds=900)))}),
        ])
        p = self._make()
        try:
            emitted_topics = []
            orig_emit = p._bus.emit

            def _wrapped_emit(topic, data, ts):
                emitted_topics.append(topic)
                return orig_emit(topic, data, ts)

            p._bus.emit = _wrapped_emit
            await p.run()
            self.assertIn(
                "CarData.z", emitted_topics,
                "the sole buffered CarData.z entry must be released by the "
                "end-of-stream flush, not silently dropped",
            )
        finally:
            p._db.close()


class FlushIsSkippedIfNoMessageEverArrives(_Harness):
    """2026-08-17-047 WB-1 resume: the old `SessionInfo.Key` gate (which
    could stay closed forever on a Key mismatch, `self._gated` stuck True)
    is gone. The universal gate is stateless per-message and defaults to a
    no-op when `scheduled_start_utc` is None (DECISIONS.md #3), so `_start_time`
    is now set unconditionally on the FIRST message the main loop ever sees
    -- there is no longer a way for a message to arrive and leave `_start_time`
    unset. The one remaining way `_start_time` stays None is that NO message
    ever reaches the loop at all (an empty/entirely-unparseable live.jsonl) --
    the flush call site (`if self._start_time is not None:`) must not assume
    a message ever arrived."""

    async def test_no_flush_call_site_crash_when_no_message_ever_arrives(self):
        (self.sess / "live.jsonl").write_text("")
        p = self._make()
        try:
            await p.run()   # must not raise
            self.assertIsNone(p._start_time)
        finally:
            p._db.close()


class EmitFlushedMessage(_Harness):
    """Direct coverage of `_emit_flushed_message` -- the shared per-message
    handling `flush()`'s release loop and the wall-clock backstop loop both
    use."""

    def setUp(self):
        super().setUp()
        self._write_lines([json.dumps({
            "Type": "SessionInfo", "DateTime": "2026-07-18T10:00:00.000Z",
            "Json": {"Key": 11330, "Type": "Practice", "Name": "Practice 1"},
        })])

    def test_message_dropped_by_filter_returns_none(self):
        p = self._make()
        try:
            p._cutoff = datetime(2026, 7, 18, 9, 0, 0, tzinfo=timezone.utc)
            stale = NormalizedMessage(
                topic="Heartbeat", data={}, envelope_ts=datetime(2026, 7, 18, 0, 0, 0, tzinfo=timezone.utc),
                utc_timestamp=datetime(2026, 7, 18, 0, 0, 0, tzinfo=timezone.utc), offset_ms=0,
            )
            self.assertIsNone(p._emit_flushed_message(stale))
        finally:
            p._db.close()

    def test_message_that_survives_filter_is_emitted_and_counted(self):
        p = self._make()
        try:
            before = p._message_count
            emitted = []
            p._bus.emit = lambda topic, data, ts: emitted.append((topic, data, ts))
            ts = datetime(2026, 7, 18, 10, 0, 5, tzinfo=timezone.utc)
            msg = NormalizedMessage(
                topic="Heartbeat", data={"n": 1}, envelope_ts=ts,
                utc_timestamp=ts, offset_ms=5000,
            )
            result = p._emit_flushed_message(msg)
            self.assertEqual(result, ts)
            self.assertEqual(p._message_count, before + 1)
            self.assertEqual(emitted, [("Heartbeat", {"n": 1}, ts)])
        finally:
            p._db.close()


class WallClockBackstopLoop(_Harness):
    """Direct coverage of `_wall_clock_backstop_loop` -- patches
    `asyncio.sleep` (this module's own import) to avoid real wall-clock
    waiting, following the same injectable-clock spirit as
    `file_reader.py`'s own `_now`/`_sleep` pattern."""

    def setUp(self):
        super().setUp()
        self._write_lines([json.dumps({
            "Type": "SessionInfo", "DateTime": "2026-07-18T10:00:00.000Z",
            "Json": {"Key": 11330, "Type": "Practice", "Name": "Practice 1"},
        })])

    async def test_loop_releases_backstop_entries_and_stops_when_not_running(self):
        p = self._make()
        try:
            p._running = True
            # This test drives `_wall_clock_backstop_loop` directly, bypassing
            # `run()`'s own gating -- must-fix 1 (2026-07-29 fix-attempt 2)
            # made the loop respect the gate/`_start_time` exactly like the
            # main loop does, so a direct-call test now needs to simulate a
            # post-gate session (the realistic case the loop actually runs
            # in -- it's only ever started from inside `run()`, after gating
            # has already begun). 2026-08-17-047 WB-1 resume: the old
            # `_gated` flag is gone -- `_start_time is not None` alone is now
            # the gate-open signal.
            p._start_time = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            ts = datetime(2026, 7, 18, 10, 0, 5, tzinfo=timezone.utc)
            call_count = {"n": 0}

            def _poll():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return [NormalizedMessage(
                        topic="Heartbeat", data={"n": 1}, envelope_ts=ts,
                        utc_timestamp=ts, offset_ms=5000,
                    )]
                # Second iteration: nothing released (exercises the "no
                # released messages" branch), then stop the loop.
                p._running = False
                return []

            p._normalizer.poll_wall_clock_backstop = _poll
            emitted = []
            p._bus.emit = lambda topic, data, ts_: emitted.append(topic)

            async def _fast_sleep(_seconds):
                return None

            with mock.patch("app.processing.preprocessor.asyncio.sleep", _fast_sleep):
                await p._wall_clock_backstop_loop()

            self.assertIn("Heartbeat", emitted)
            self.assertEqual(p._latest_backstop_ts, ts)
            self.assertGreaterEqual(call_count["n"], 2)
        finally:
            p._db.close()

    async def test_loop_logs_and_continues_on_poll_exception(self):
        p = self._make()
        try:
            p._running = True
            p._start_time = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            call_count = {"n": 0}

            def _poll():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise RuntimeError("boom")
                p._running = False
                return []

            p._normalizer.poll_wall_clock_backstop = _poll

            async def _fast_sleep(_seconds):
                return None

            with mock.patch("app.processing.preprocessor.asyncio.sleep", _fast_sleep):
                await p._wall_clock_backstop_loop()   # must not raise

            self.assertGreaterEqual(call_count["n"], 2)
        finally:
            p._db.close()

    async def test_loop_drops_released_entries_while_still_gated(self):
        """Must-fix 1 (2026-07-29 fix-attempt 2, regression coverage): a
        wall-clock backstop release that fires while the session hasn't yet
        gated open (`_start_time=None` -- e.g. a live session that hasn't
        seen any gate-surviving message yet, for longer than the backstop
        duration) must NOT reach `_bus.emit` -- this is precisely the
        zombie-message-guard bypass must-fix 1 closes. Before the fix,
        `poll_wall_clock_backstop()`'s released entries were emitted
        unconditionally regardless of gate state. 2026-08-17-047 WB-1
        resume: the old separate `_gated` flag is gone -- `_start_time is
        None` alone is now the "hasn't gated open yet" signal (this test
        never calls `run()`, so `_start_time` is naturally still unset)."""
        p = self._make()
        try:
            p._running = True
            self.assertIsNone(p._start_time)
            ts = datetime(2026, 7, 18, 10, 0, 5, tzinfo=timezone.utc)
            call_count = {"n": 0}

            def _poll():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return [NormalizedMessage(
                        topic="CarData.z", data={"n": 1}, envelope_ts=ts,
                        utc_timestamp=ts, offset_ms=5000,
                    )]
                p._running = False
                return []

            p._normalizer.poll_wall_clock_backstop = _poll
            emitted = []
            p._bus.emit = lambda topic, data, ts_: emitted.append(topic)

            async def _fast_sleep(_seconds):
                return None

            with mock.patch("app.processing.preprocessor.asyncio.sleep", _fast_sleep):
                await p._wall_clock_backstop_loop()

            self.assertEqual(
                emitted, [],
                "a backstop-released entry must be dropped, not emitted, "
                "while the session is still gated",
            )
            self.assertIsNone(
                p._latest_backstop_ts,
                "a dropped (gated) release must not advance the backstop "
                "timestamp watermark either",
            )
        finally:
            p._db.close()

    async def test_loop_second_later_message_wins_the_latest_backstop_ts_merge(self):
        """Coverage: `_wall_clock_backstop_loop`'s per-message merge
        (`ts is not None and (self._latest_backstop_ts is None or ts >
        self._latest_backstop_ts)`) needs BOTH the True arm (a genuinely
        later timestamp updates the watermark -- ts2 vs ts1 below) and the
        False arm (a timestamp that is NOT later leaves the watermark
        unchanged and the loop continues to the next message -- ts3 vs ts2
        below) exercised; the first released message in a run always takes
        the `is None` sub-arm alone, so a 3rd, non-later message is needed
        to reach the False arm of the overall condition at all."""
        p = self._make()
        try:
            p._running = True
            p._start_time = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            ts1 = datetime(2026, 7, 18, 10, 0, 5, tzinfo=timezone.utc)
            ts2 = datetime(2026, 7, 18, 10, 0, 6, tzinfo=timezone.utc)
            ts3 = datetime(2026, 7, 18, 10, 0, 6, tzinfo=timezone.utc)  # not later than ts2
            call_count = {"n": 0}

            def _poll():
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # Three entries released in the SAME poll: ts1 (sets the
                    # watermark via the `is None` sub-arm), ts2 (a genuinely
                    # later timestamp -- the `ts > ...` True arm), ts3 (NOT
                    # later than ts2 -- the False arm, watermark unchanged).
                    return [
                        NormalizedMessage(topic="Heartbeat", data={"n": 1},
                                          envelope_ts=ts1, utc_timestamp=ts1, offset_ms=5000),
                        NormalizedMessage(topic="Heartbeat", data={"n": 2},
                                          envelope_ts=ts2, utc_timestamp=ts2, offset_ms=6000),
                        NormalizedMessage(topic="Heartbeat", data={"n": 3},
                                          envelope_ts=ts3, utc_timestamp=ts3, offset_ms=6000),
                    ]
                p._running = False
                return []

            p._normalizer.poll_wall_clock_backstop = _poll
            emitted = []
            p._bus.emit = lambda topic, data, ts_: emitted.append(topic)

            async def _fast_sleep(_seconds):
                return None

            with mock.patch("app.processing.preprocessor.asyncio.sleep", _fast_sleep):
                await p._wall_clock_backstop_loop()

            self.assertEqual(emitted, ["Heartbeat", "Heartbeat", "Heartbeat"])
            self.assertEqual(
                p._latest_backstop_ts, ts2,
                "ts3 is not later than ts2 -- the watermark must stay at ts2",
            )
        finally:
            p._db.close()


class BackstopTaskLifecycle(_Harness):
    """Coverage for the task-creation (tail_follow branch) and
    cancel-before-flush wiring in `run()` itself."""

    def setUp(self):
        super().setUp()
        base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        self._write_lines([
            json.dumps({"Type": "SessionInfo", "DateTime": _iso(base), "Json": si}),
        ])

    async def test_tail_follow_run_creates_and_cleanly_cancels_backstop_task(self):
        p = self._make()
        try:
            # Stop-follow is already set, so the very first EOF poll in
            # tail-follow mode breaks immediately (file_reader.py) -- no
            # real wall-clock wait, keeping this test fast.
            p._stop_follow.set()
            await p.run(tail_follow=True)   # must not hang or raise
            self.assertEqual(p._db.get_meta("status"), "complete")
        finally:
            p._db.close()

    async def test_exception_mid_loop_still_cancels_a_lingering_backstop_task(self):
        """An exception raised before the normal cancel-then-flush point in
        run() (e.g. a processor blowing up on a post-gate message) must
        still leave no backstop task running behind it -- the `finally`
        safety net, not just the normal-path cancellation."""
        base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        self._write_lines([
            json.dumps({"Type": "SessionInfo", "DateTime": _iso(base), "Json": si}),
            # A second, post-gate message -- SessionInfo itself is emitted
            # directly at gate-open without going through
            # `_discover_topic`, so this is needed to actually reach the
            # patched call below.
            json.dumps({"Type": "Heartbeat", "DateTime": _iso(base + timedelta(seconds=1)), "Json": {}}),
        ])
        p = self._make()
        try:
            p._stop_follow.set()   # belt-and-braces: don't hang if the exception is never reached

            def _boom(topic):
                raise RuntimeError("boom")

            p._discover_topic = _boom
            with self.assertRaises(RuntimeError):
                await p.run(tail_follow=True)
            self.assertTrue(p.failed)
        finally:
            p._db.close()


class BackstopMergeAndFlushLoopBranches(_Harness):
    """Coverage-closing tests for two narrow branches in `run()` itself: the
    backstop timestamp winning the merge into `last_ts` (line ~546), and the
    explicit flush loop breaking mid-way when `self._running` flips False
    (line ~562) -- neither is reachable from a realistic small fixture
    without direct control over timing, so both are driven with targeted
    monkeypatches rather than a real multi-second wait."""

    def setUp(self):
        super().setUp()
        base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        self._write_lines([
            json.dumps({"Type": "SessionInfo", "DateTime": _iso(base), "Json": si}),
        ])

    async def test_backstop_timestamp_wins_the_merge_into_last_ts(self):
        p = self._make()
        try:
            future_ts = datetime(2026, 7, 18, 11, 0, 0, tzinfo=timezone.utc)

            async def _fake_backstop_loop():
                p._latest_backstop_ts = future_ts
                try:
                    await asyncio.sleep(1e9)   # parked until run() cancels it
                except asyncio.CancelledError:
                    raise

            p._wall_clock_backstop_loop = _fake_backstop_loop
            p._stop_follow.set()
            await p.run(tail_follow=True)
            self.assertEqual(p._latest_backstop_ts, future_ts)
            self.assertEqual(p._db.get_meta("status"), "complete")
        finally:
            p._db.close()

    async def test_flush_loop_breaks_when_running_flips_false_mid_iteration(self):
        p = self._make()
        try:
            base = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            two_entries = [
                NormalizedMessage(
                    topic="Heartbeat", data={"n": 1}, envelope_ts=base,
                    utc_timestamp=base, offset_ms=0),
                NormalizedMessage(
                    topic="Heartbeat", data={"n": 2}, envelope_ts=base + timedelta(seconds=1),
                    utc_timestamp=base + timedelta(seconds=1), offset_ms=1000),
            ]
            p._normalizer.flush = lambda: two_entries

            orig_emit_flushed = p._emit_flushed_message
            calls = {"n": 0}

            def _emit_flushed_then_stop(msg):
                calls["n"] += 1
                if calls["n"] == 1:
                    p._running = False
                return orig_emit_flushed(msg)

            p._emit_flushed_message = _emit_flushed_then_stop
            await p.run()
            # Only the FIRST buffered entry is processed -- the loop must
            # break, not continue, once _running flips False mid-iteration.
            self.assertEqual(calls["n"], 1)
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
