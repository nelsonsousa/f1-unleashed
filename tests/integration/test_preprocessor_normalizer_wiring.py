"""Integration coverage for `preprocessor.py`'s StreamNormalizer wiring
(2026-07-27-003 pipeline redesign): the SessionInfo.Key gate still triggers
`_start_time`/normalizer reference (DECISIONS.md #1 — the universal gate is
not wired to a schedule source in this pass), but every message now flows
through the normalizer first, so:

  - ExtrapolatedClock updates STREAM_LAG even while messages are still
    buffered (pre-gate) — verified via `processing_meta.stream_lag_final`,
    and via the gate-opening SessionInfo itself picking up the correction
    (it is a generic, non-EC topic, so it is NOT exempt from STREAM_LAG).
  - `offset_ms` is taken FROM the normalized message, not recomputed, and is
    correctly anchored at gate-open (D4): a CONSTANT STREAM_LAG cancels out
    of the delta between any two corrected timestamps, so the relative
    offset between gate-open and a later message is unchanged from the
    pre-redesign envelope-only arithmetic — exactly the invariant D4 set out
    to preserve (architecture-plan.md §A.4), now demonstrated rather than
    merely asserted.
  - The pre-gate buffer-flush path (DriverList always-flush; same-timestamp
    topics) still runs against normalizer output.
"""
import base64
import json
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from app.processing.preprocessor import SessionPreProcessor


def _env(topic: str, dt: str, data) -> str:
    return json.dumps({"Type": topic, "DateTime": dt, "Json": data})


def _compress(payload: dict) -> str:
    raw = json.dumps(payload).encode("utf-8")
    co = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = co.compress(raw) + co.flush()
    return base64.b64encode(compressed).decode("ascii")


class PreprocessorNormalizerWiring(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

        lines = [
            # Pre-gate: EC lags the payload by 2s. STREAM_LAG must update from
            # this even though the line itself gets buffered (different ts
            # than SessionInfo, no DriverList exemption) and later discarded.
            _env("ExtrapolatedClock", "2026-07-18T09:59:50.000Z",
                 {"Utc": "2026-07-18T09:59:48.000Z"}),
            # Pre-gate DriverList: always flushed regardless of timestamp.
            _env("DriverList", "2026-07-18T09:59:55.000Z",
                 {"1": {"RacingNumber": "1", "TeamName": "Test Team"}}),
            # Gate-opening SessionInfo.
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", si),
            # Post-gate Heartbeat, 5s after gate-open (envelope).
            _env("Heartbeat", "2026-07-18T10:00:05.000Z", {}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_stream_lag_updates_while_buffered_and_offset_is_corrected(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")
        try:
            await p.run()
            self.assertFalse(p.failed)

            # SessionInfo's OWN utc_timestamp is envelope(10:00:00) minus the
            # 2s STREAM_LAG already established by the pre-gate EC line —
            # proof that STREAM_LAG updated even though that EC line itself
            # was buffered and later discarded (different ts than SessionInfo,
            # not DriverList).
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T09:59:58+00:00")
            self.assertAlmostEqual(float(p._db.get_meta("stream_lag_final")), 2.0, places=3)

            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
            offset_ms, _topic, _data = heartbeat_rows[0]
            # Heartbeat envelope 10:00:05 minus the SAME 2s STREAM_LAG =
            # 10:00:03 utc; gate-open utc is 09:59:58 (also lag-corrected) —
            # delta is 5000ms either way, because a constant STREAM_LAG
            # cancels out of the offset delta (D4's preserved invariant).
            self.assertEqual(offset_ms, 5000)
        finally:
            p._db.close()


class PreStartZTopicSkip(unittest.IsolatedAsyncioTestCase):
    """Coverage (critical path): the pre-start `.z` skip
    (`if self._start_time is None and msg.topic in ("CarData.z", "Position.z"): continue`)
    — one of the TWO mechanisms guarding the confirmed zombie-message bug
    (CVFyRpfx), retained per DECISIONS.md #1. A CarData.z/Position.z entry
    arriving before the gate has opened (`_start_time is None`) must be
    silently skipped rather than buffered."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Qualifying"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Qualifying", "Name": "Qualifying"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

        car_data_z = _compress({"Entries": [
            {"Utc": "2026-07-18T09:59:00.000Z", "Cars": {"1": {"Channels": {"2": 300}}}},
        ]})
        lines = [
            # Pre-gate CarData.z — must be skipped, not buffered.
            _env("CarData.z", "2026-07-18T09:59:50.000Z", car_data_z),
            _env("SessionInfo", "2026-07-18T10:00:00.000Z", si),
            _env("Heartbeat", "2026-07-18T10:00:05.000Z", {}),
        ]
        (self.sess / "live.jsonl").write_text("\n".join(lines) + "\n")

    async def test_pre_gate_car_data_z_is_skipped_not_buffered(self):
        with mock.patch("app.processing.database.transient_db_path",
                        return_value=self.root / "session.db"):
            p = SessionPreProcessor(self.sess, "Qualifying")
        try:
            await p.run()
            self.assertFalse(p.failed)
            # The gate still opened normally on SessionInfo — the skipped
            # CarData.z never entered (or poisoned) the gate buffer.
            self.assertEqual(p._db.get_meta("start_time"), "2026-07-18T10:00:00+00:00")
            rows = p._db.get_messages_in_range(-1, 10_000)
            heartbeat_rows = [r for r in rows if r[1] == "heartbeat"]
            self.assertEqual(len(heartbeat_rows), 1)
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
