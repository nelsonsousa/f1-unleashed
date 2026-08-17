"""Integration coverage for `preprocessor.py`'s StreamNormalizer wiring
(2026-07-27-003 pipeline redesign, completed 2026-08-17-047 WB-1 resume):
`StreamNormalizer`'s universal 60-minute-before-scheduled-start gate (§9.2)
is now the ONLY gate -- the old `SessionInfo.Key` exact-match buffer/gate
this file used to document (DECISIONS.md #1's now-completed interim state)
is gone, and so is the pre-start `.z` topic skip (DECISIONS.md #1's second
redundant mechanism). This file is the canary that DECISIONS.md #1 actually
landed: it used to say "the SessionInfo.Key gate still triggers"; it now
proves the universal gate is the one and only thing deciding what survives.

  - ExtrapolatedClock updates STREAM_LAG for the FIRST message the loop ever
    sees (there is no more pre-gate buffering phase to update it "early" in)
    -- verified via `processing_meta.stream_lag_final`.
  - `offset_ms` is taken FROM the normalized message, not recomputed, and is
    correctly anchored at the first gate-survivor (D4): a CONSTANT
    STREAM_LAG cancels out of the delta between any two corrected
    timestamps, so the relative offset between the first survivor and a
    later message is unchanged from the pre-redesign envelope-only
    arithmetic -- exactly the invariant D4 set out to preserve
    (architecture-plan.md §A.4), now achieved via the normalizer's own
    auto-reference (DECISIONS.md #2's completion) rather than an explicit
    `set_reference()` override from `preprocessor.py`.
  - A message whose payload timestamp is more than 60 minutes before a REAL
    `scheduled_start_utc` is dropped by the universal gate before
    `preprocessor.py` ever sees it -- replacing BOTH of the two old
    mechanisms (SessionInfo.Key gate-buffer AND the pre-start `.z` skip)
    with the one rule §9.2 specifies.
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


class PreprocessorNormalizerWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

        lines = [
            # FIRST message the loop sees: ExtrapolatedClock, lagging the
            # payload by 2s. STREAM_LAG must be established from this very
            # first message (no more "buffered, updates STREAM_LAG before
            # gate-open" distinction -- there is no buffering phase left).
            _env("ExtrapolatedClock", "2026-07-18T09:59:50.000Z",
                 {"Utc": "2026-07-18T09:59:48.000Z"}),
            # Second message, 5s after the first (envelope).
            _env("Heartbeat", "2026-07-18T09:59:55.000Z", {}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_stream_lag_updates_from_first_message_and_offset_is_correct(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")
        try:
            await p.run()
            self.assertFalse(p.failed)

            # ExtrapolatedClock's OWN utc_timestamp is its envelope
            # (09:59:50) minus the 2s STREAM_LAG its own payload just
            # established -- it becomes the first gate-survivor and
            # `_start_time` in the SAME step that computes the lag
            # correction for itself (architecture-plan.md §A.3.2 step 3).
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T09:59:48+00:00")
            self.assertAlmostEqual(float(p._db.get_meta("stream_lag_final")), 2.0, places=3)

            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
            offset_ms, _topic, _data = heartbeat_rows[0]
            # Heartbeat envelope 09:59:55 minus the SAME 2s STREAM_LAG =
            # 09:59:53 utc; first-survivor utc is 09:59:48 (also
            # lag-corrected) -- delta is 5000ms either way, because a
            # constant STREAM_LAG cancels out of the offset delta (D4's
            # preserved invariant, now achieved via the normalizer's own
            # auto-reference rather than preprocessor.py's removed
            # `set_reference()` override).
            self.assertEqual(offset_ms, 5000)
        finally:
            p._db.close()


class UniversalGateReplacesBothOldMechanisms(unittest.IsolatedAsyncioTestCase):
    """Coverage (critical path): the universal 60-minute-before-scheduled-
    start gate (`StreamNormalizer._gate()`, §9.2) is now the SOLE mechanism
    guarding the confirmed zombie-message bug (CVFyRpfx) -- it replaces both
    the old `SessionInfo.Key` gate-buffer and the old pre-start `.z` topic
    skip (both removed, DECISIONS.md #1's completion). A CarData.z entry
    whose payload timestamp is more than 60 minutes before a REAL
    `scheduled_start_utc` must be dropped before `preprocessor.py` ever sees
    it -- with no separate `.z`-specific skip needed to also catch it."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))
        self.scheduled_start_utc = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)

        import base64
        import zlib

        def _compress(payload: dict) -> str:
            raw = json.dumps(payload).encode("utf-8")
            co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
            compressed = co.compress(raw) + co.flush()
            return base64.b64encode(compressed).decode("ascii")

        # Payload timestamp is 2 HOURS before scheduled_start_utc -- well
        # outside the 60-minute GATE_WINDOW. Must be dropped entirely.
        car_data_z = _compress({"Entries": [
            {"Utc": "2026-07-18T08:00:00.000Z", "Cars": {"1": {"Channels": {"2": 300}}}},
        ]})
        lines = [
            _env("CarData.z", "2026-07-18T08:00:01.000Z", car_data_z),
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", si),
            _env("Heartbeat", "2026-07-18T10:00:05.000Z", {}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_pre_scheduled_start_car_data_z_is_gated_out_by_the_universal_gate(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(
                self.sess, "Qualifying",
                scheduled_start_utc=self.scheduled_start_utc,
            )
        try:
            await p.run()
            self.assertFalse(p.failed)
            # The gate-dropped CarData.z never reached the loop -- SessionInfo
            # (the first SURVIVING message) becomes `_start_time`, not the
            # earlier-arriving-but-gated-out CarData.z.
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T10:00:00+00:00")
            self.assertEqual(p._db.get_meta("message_count"), "2")  # SessionInfo + Heartbeat
            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
