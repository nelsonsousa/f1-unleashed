"""Trello card 7g6yuitv -- RED GATE / GREEN GATE.

`SessionMessageBus.emit` (app/processing/message_bus.py) caught every
individual processor-handler exception internally (`except Exception:
logger.exception(...)`) and continued -- a bug inside any one processor
(app/processing/processors/*.py) could never reach
`SessionPreProcessor.run()`'s own exception handler, regardless of WB-4's fix
(tests/test_wb4_preprocessor_run_failure_semantics.py), which only covers
preprocessor.py's OWN file-I/O/bookkeeping code, not a processor bug routed
through `emit()`.

Decision (human, 2026-08-19, not re-litigated here): a broken processor must
propagate its exception and fail the whole build, the same as WB-4 already
established for preprocessor.py's own errors -- deliberately the stricter of
the two options on the table (isolate-and-surface-more-visibly was NOT
chosen).

RED GATE (against unfixed code, see report/commit for the captured failure
output): every test below fails, because emit() currently catches the
handler's exception, logs it, and returns normally -- `assertRaises` never
sees a raise, and the fail-fast/persist-sink-skipped assertions cannot even
be reached.

GREEN GATE (after the fix): a processor-handler exception now propagates out
of emit(), fail-fast among handlers for the same topic -- matching WB-4's own
"raise, don't swallow" contract for preprocessor.py's file-I/O/bookkeeping
errors, reusing the same downstream mechanism (run()'s `except Exception:
... raise`).
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus


class EmitPropagatesHandlerExceptions(unittest.TestCase):
    def setUp(self):
        self.bus = SessionMessageBus()
        self.t0 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)

    def test_handler_exception_propagates_out_of_emit(self):
        def _boom(data, clock_time):
            raise RuntimeError("simulated processor bug")

        self.bus.on("TrackStatus", _boom)

        with self.assertRaises(RuntimeError) as ctx:
            self.bus.emit("TrackStatus", {"Status": "1"}, self.t0)
        self.assertIn("simulated processor bug", str(ctx.exception))

    def test_fail_fast_second_handler_for_same_topic_does_not_run(self):
        """Multiple processors subscribed to the same topic: once the first
        raises, the second must not run -- fail fast, not isolate-and-
        continue. No existing precedent favours "continue remaining
        handlers, then raise" (WB-4's own re-raise is a single call site,
        not a fan-out loop), so the simpler/safer fail-fast option is taken
        -- see report for the reasoning."""
        second_handler_called = []

        def _boom(data, clock_time):
            raise RuntimeError("first handler bug")

        def _second(data, clock_time):
            second_handler_called.append(True)

        self.bus.on("TrackStatus", _boom)
        self.bus.on("TrackStatus", _second)

        with self.assertRaises(RuntimeError):
            self.bus.emit("TrackStatus", {"Status": "1"}, self.t0)

        self.assertEqual(
            second_handler_called, [],
            "a later handler for the same topic ran after an earlier one "
            "raised -- fail-fast was not honoured"
        )

    def test_persist_sink_does_not_run_when_a_handler_raises(self):
        """emit() must raise before reaching the persist sink -- a message
        whose processing blew up must not still be persisted as if it
        succeeded."""
        persisted = []

        def _boom(data, clock_time):
            raise RuntimeError("processor bug")

        self.bus.on("TrackStatus", _boom)
        self.bus.set_persist_sink(
            lambda topic, data, clock_time: persisted.append((topic, data))
        )

        with self.assertRaises(RuntimeError):
            self.bus.emit("TrackStatus", {"Status": "1"}, self.t0)

        self.assertEqual(persisted, [], "persist sink ran despite a handler exception")

    def test_wildcard_handler_exception_also_propagates(self):
        def _boom(topic, data, clock_time):
            raise RuntimeError("wildcard handler bug")

        self.bus.on("*", _boom)

        with self.assertRaises(RuntimeError):
            self.bus.emit("AnyTopic", {"x": 1}, self.t0)

    def test_handler_exception_is_logged_with_topic_context_before_propagating(self):
        """Useful context (which topic) must still be logged -- a bare
        re-raise loses that at the point run() catches it, so emit() itself
        must log it before the exception leaves the function."""
        def _boom(data, clock_time):
            raise RuntimeError("simulated processor bug")

        self.bus.on("TrackStatus", _boom)

        with self.assertLogs("app.processing.message_bus", level="ERROR") as log:
            with self.assertRaises(RuntimeError):
                self.bus.emit("TrackStatus", {"Status": "1"}, self.t0)
        self.assertTrue(
            any("TrackStatus" in m for m in log.output),
            f"expected the topic name in the logged error, got: {log.output}"
        )

    def test_handler_for_unrelated_topic_is_unaffected(self):
        """Sanity: a topic with no raising handler still emits normally --
        this change must not break the ordinary, non-failing path."""
        received = []
        self.bus.on("Heartbeat", lambda data, ct: received.append(data))
        self.bus.emit("Heartbeat", {"beat": 1}, self.t0)
        self.assertEqual(received, [{"beat": 1}])


if __name__ == "__main__":
    unittest.main()
