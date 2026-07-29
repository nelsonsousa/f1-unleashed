"""AC-3's end-of-stream flush (requirement-spec.md AC-3; file-impact-map.md
§1's correction): must be hooked to EXHAUSTION OF THE ASYNC ITERATOR in
`preprocessor.py`'s main loop — NOT the `_SessionEnd` marker (swallowed
inside `file_reader.py`, inert during live tail-follow) — and must run
BEFORE `self._telem_proc.finalize_session(last_dt)` (preprocessor.py:521),
since `finalize_session` closes whatever lap is open using whatever is in
`drv.samples` at that moment.

NO IMPLEMENTATION EXISTS YET: no reorder buffer, no `flush()` method on
`StreamNormalizer`, and no call site in `preprocessor.py`'s main loop before
`finalize_session`. ASSUMED INTERFACE (see
tests/unit/test_stream_normalizer_reorder_buffer.py's own assumed-interface
note): `StreamNormalizer.flush()` returning whatever `NormalizedMessage`s
remain buffered.

WHY THIS TEST IS SHAPED AS A WIRING CONTRACT, NOT AN END-TO-END PAIRING
ASSERTION: the buffer/flush's observable effect (a held entry surviving to
EOF) only exists once AC-1's buffer exists at all. Against TODAY's
(unbuffered) code, nothing is ever held — every `.z` entry passes straight
through `StreamNormalizer.process_line()` immediately regardless of file
position, so an end-to-end pairing assertion at EOF would (if position-
processor geometry were also stood up) trivially PASS today for the wrong
reason (nothing was ever at risk of being lost, because nothing is ever
buffered) — exactly the "test that fails/passes for an incidental reason"
rules/testing.md warns against. This test instead asserts the WIRING
CONTRACT directly: `preprocessor.py`'s main loop must call
`normalizer.flush()` (the assumed interface) once input is exhausted, and
must do so BEFORE `finalize_session` runs. That is testable and meaningfully
red today regardless of whether the buffer itself exists yet, because the
call site simply does not exist."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor
from app.processing.stream_normalizer import StreamNormalizer, NormalizedMessage
from datetime import datetime, timezone


class FlushIsCalledBeforeFinalizeSession_AssumedInterface(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Practice"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))
        lines = [
            json.dumps({"Type": "SessionInfo", "DateTime": "2026-07-18T10:00:00.000Z", "Json": si}),
            json.dumps({"Type": "Heartbeat", "DateTime": "2026-07-18T10:00:01.000Z", "Json": {}}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_normalizer_flush_is_invoked_before_finalize_session_RED_GATE(self):
        call_order = []

        def _flush_side_effect(self_norm):
            call_order.append("flush")
            return []

        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Practice")
        try:
            with mock.patch.object(StreamNormalizer, "flush",
                                    side_effect=_flush_side_effect, autospec=True):
                # _init_processors() runs inside run(); patch finalize_session
                # on the class so any instance created during run() is covered.
                from app.processing.processors.telemetry_processor import TelemetryProcessor
                orig = TelemetryProcessor.finalize_session

                def _wrapped_finalize(self_tp, clock_time):
                    call_order.append("finalize_session")
                    return orig(self_tp, clock_time)

                with mock.patch.object(TelemetryProcessor, "finalize_session", _wrapped_finalize):
                    await p.run()

            self.assertIn(
                "flush", call_order,
                "preprocessor.py's main loop never calls StreamNormalizer.flush() "
                "(assumed interface) — AC-3's end-of-stream flush, hooked to "
                "async-iterator exhaustion, has not been implemented yet",
            )
            if "flush" in call_order and "finalize_session" in call_order:
                self.assertLess(
                    call_order.index("flush"), call_order.index("finalize_session"),
                    "flush() must run BEFORE finalize_session — finalize_session "
                    "closes the open lap using whatever is in drv.samples at "
                    "that moment (file-impact-map.md §1 AC-3)",
                )
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
