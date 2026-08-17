"""Continuous dedup (§9.1) vs. gating, reworked for 2026-08-17-047 WB-1
resume (DECISIONS.md #1's completion).

## What this file used to test

Pre-fix, `StreamNormalizer`'s dedup ran on every line as it was produced
(inside `process_line`), BEFORE `preprocessor.py`'s own `SessionInfo.Key`
gate-buffer logic decided whether that line would ever actually be forwarded
to the bus. A duplicate-content baseline-state message that arrived pre-gate
and got buffered-then-discarded (never reaching the bus) had already
consumed the normalizer's "last emitted" dedup state for its topic -- the
REAL subsequent message with the same content was then wrongly suppressed as
a duplicate. The fix at the time was `preprocessor.py` explicitly disabling
the normalizer's dedup (`set_dedup_enabled(False)`) for the pre-gate
buffering window and re-enabling it at gate-open.

## What changed and why this file's old premise is gone

This task removed BOTH `set_dedup_enabled()` calls from `preprocessor.py`
entirely (implementation-plan.md, "`set_dedup_enabled` (file-impact-map
§2.1's flagged-but-unscoped item)") -- dedup is now always-on for the whole
`SessionPreProcessor` lifetime. This is safe, not a regression, because the
underlying cause of the old bug is structurally gone: `preprocessor.py` no
longer has a "provisionally process now, decide whether to forward later"
buffering phase at all. `StreamNormalizer.process_line` itself now runs the
universal gate (`_gate()`) BEFORE dedup (`stream_normalizer.py:356-373`) --
confirmed directly by reading the source, not assumed. A message the gate
drops is never even considered for dedup (`if gated is None: return []`,
before the dedup block), so a gated-out message can no longer poison dedup
state for a later real one -- there is no code path left where dedup state
is updated by a message that will not, in fact, be forwarded.

The two tests below replace this file's old single scenario with direct
coverage of that structural guarantee, plus confirming dedup is genuinely
always-on now (a true content duplicate, with nothing pre-session about it,
IS suppressed -- proving dedup wasn't accidentally left disabled).
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


class PreScheduledStartDuplicateCannotPoisonDedupState(unittest.IsolatedAsyncioTestCase):
    """A duplicate-content message arriving well before `scheduled_start_utc`
    (outside the universal gate's 60-minute window) is dropped by the gate --
    BEFORE dedup ever sees it (`stream_normalizer.py`'s gate-then-dedup
    order). The real, post-gate message with the same content must still
    reach the bus: the gate-dropped duplicate never touched dedup state to
    poison it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        self.si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": self.si}))
        self.scheduled_start_utc = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)

    async def test_gate_dropped_pre_session_duplicate_does_not_suppress_the_real_one(self):
        lines = [
            # 2 hours before scheduled_start_utc -- outside the 60-minute
            # gate window, dropped before dedup ever runs on it.
            _env("TrackStatus", "2026-07-18T08:00:00.000Z", {"Status": "1", "Message": "AllClear"}),
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", self.si),
            # The REAL TrackStatus -- same content as the gate-dropped one
            # above -- arriving after the gate has opened.
            _env("TrackStatus", "2026-07-18T10:00:02.000Z", {"Status": "1", "Message": "AllClear"}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(
                self.sess, "Qualifying",
                scheduled_start_utc=self.scheduled_start_utc,
            )

        emitted_track_status = []
        real_emit = p._bus.emit

        def _spy_emit(topic, data, clock_time):
            if topic == "TrackStatus":
                emitted_track_status.append(data)
            return real_emit(topic, data, clock_time)

        p._bus.emit = _spy_emit

        try:
            await p.run()
            self.assertFalse(p.failed)

            self.assertEqual(len(emitted_track_status), 1)
            self.assertEqual(emitted_track_status[0], {"Status": "1", "Message": "AllClear"})
        finally:
            p._db.close()


class DedupIsAlwaysOnNowNotAccidentallyDisabled(unittest.IsolatedAsyncioTestCase):
    """A GENUINE content duplicate (both messages post-gate, nothing
    pre-session about either) must still be suppressed by the normalizer's
    continuous dedup rule (§9.1) -- proving `preprocessor.py`'s removal of
    both `set_dedup_enabled()` calls left dedup ENABLED throughout, not
    accidentally disabled altogether."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        self.si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": self.si}))

    async def test_post_gate_duplicate_content_is_suppressed(self):
        lines = [
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", self.si),
            _env("TrackStatus", "2026-07-18T10:00:02.000Z", {"Status": "1", "Message": "AllClear"}),
            # Identical content, arriving again a moment later -- a genuine
            # resend, not a pre-gate artifact.
            _env("TrackStatus", "2026-07-18T10:00:04.000Z", {"Status": "1", "Message": "AllClear"}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")

        emitted_track_status = []
        real_emit = p._bus.emit

        def _spy_emit(topic, data, clock_time):
            if topic == "TrackStatus":
                emitted_track_status.append(data)
            return real_emit(topic, data, clock_time)

        p._bus.emit = _spy_emit

        try:
            await p.run()
            self.assertFalse(p.failed)
            self.assertEqual(
                len(emitted_track_status), 1,
                "the second, identical-content TrackStatus must be "
                "suppressed by continuous dedup -- dedup is always-on now, "
                "not accidentally disabled by the set_dedup_enabled() removal",
            )
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
