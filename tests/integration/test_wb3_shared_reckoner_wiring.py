"""AC-5 (`requirement-spec.md`, "single reckoner, not two") -- `preprocessor.py`'s WB3 wiring
(`docs/artifacts/2026-08-01-040-merged-position-telemetry-processor/` implementation-plan.md
"WB3", §3's `preprocessor.py` row): `PositionProcessor` and `TelemetryProcessor`, constructed
through `SessionPreProcessor._init_processors`'s ACTUAL init path (not a hand-rolled stand-in),
must share the SAME `DpReckoner` object -- not two independently-constructed instances that
happen to behave alike.

This is the one place that makes AC-2's cross-processor calibration/anchor sharing real rather
than theoretical: `test_telemetry_processor_wb3_dp_reckoning.py` proves the SPLIT LOGIC is
correct given a shared reckoner, constructed by hand; this file proves `preprocessor.py` actually
DELIVERS a shared reckoner to both processors in production, and that `PositionProcessor` is
registered (and therefore dispatched) BEFORE `TelemetryProcessor` on `CarData.z` -- the ordering
`dp_reckoner.py`'s own "IMPORTANT for WB3" docstring note and `telemetry_processor.py`'s
last-entry `current_dp()` carve-out both depend on.

Uses the same `SessionPreProcessor` construction pattern as
`tests/unit/test_preprocessor_ac3_wiring.py` (temp session dir + patched `transient_db_path`) --
`_init_processors()` itself has no gating dependency, so it can be exercised directly without a
full `run()`.
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app.processing.dp_reckoner import DpReckoner
from app.processing.preprocessor import SessionPreProcessor
from app.processing.processors.position_processor import PositionProcessor
from app.processing.processors.telemetry_processor import TelemetryProcessor


class _Harness(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.sess = self.root / "2026" / "1290_Test" / "11330_Practice"
        self.sess.mkdir(parents=True)
        si = {"Key": 11330, "Type": "Practice", "Name": "Practice 1"}
        (self.sess / "subscribe.json").write_text(json.dumps({"SessionInfo": si}))

    def _make(self) -> SessionPreProcessor:
        with mock.patch("app.processing.database.transient_db_path",
                         return_value=self.root / "session.db"):
            return SessionPreProcessor(self.sess, "Practice")


class SharedReckonerInstanceIdentity(_Harness):
    def test_position_and_telemetry_processors_share_one_dpreckoner_instance(self):
        p = self._make()
        try:
            p._init_processors()

            position_procs = [x for x in p._processors if isinstance(x, PositionProcessor)]
            self.assertEqual(len(position_procs), 1, "exactly one PositionProcessor must be registered")
            pos_proc = position_procs[0]

            self.assertIsInstance(p._telem_proc, TelemetryProcessor)
            self.assertIn(p._telem_proc, p._processors, "telem_proc must be the SAME object registered in _processors")

            self.assertIsInstance(pos_proc._reckoner, DpReckoner)
            self.assertIsInstance(p._telem_proc._reckoner, DpReckoner)
            self.assertIs(
                pos_proc._reckoner, p._telem_proc._reckoner,
                "PositionProcessor and TelemetryProcessor must share the SAME DpReckoner "
                "object, not two independently-constructed instances (AC-5)",
            )
        finally:
            p._db.close()

    def test_two_separate_sessions_get_two_independent_reckoners(self):
        """Sanity check the identity assertion above is actually meaningful:
        a SECOND SessionPreProcessor must NOT accidentally share state with
        the first (e.g. via a module-level singleton) -- each session's
        `_init_processors()` call constructs its own fresh `DpReckoner`."""
        p1 = self._make()
        p2 = self._make()
        try:
            p1._init_processors()
            p2._init_processors()
            self.assertIsNot(
                p1._telem_proc._reckoner, p2._telem_proc._reckoner,
                "two different SessionPreProcessor instances must not share a reckoner",
            )
        finally:
            p1._db.close()
            p2._db.close()


class SharedReckonerStateIsGenuinelyVisibleAcrossProcessors(_Harness):
    """Identity alone (`is`) proves the objects are the same; this class
    proves the SHARING actually does something observable -- a real fix fed
    to the shared reckoner via PositionProcessor's own object is visible
    through TelemetryProcessor's `last_known_dp`/`ms_since_last_known`
    accessors on the SAME object, with no re-derivation."""

    def test_a_real_fix_observed_via_the_positionprocessor_reckoner_is_visible_via_telemetryprocessor(self):
        p = self._make()
        try:
            p._init_processors()
            pos_proc = next(x for x in p._processors if isinstance(x, PositionProcessor))
            telem_proc = p._telem_proc

            self.assertIsNone(telem_proc._reckoner.last_known_dp("44"))

            ts = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            pos_proc._reckoner.observe_real_position("44", 37.5, ts)

            self.assertEqual(
                telem_proc._reckoner.last_known_dp("44"), 37.5,
                "a real fix fed through PositionProcessor's own reckoner reference "
                "must be visible through TelemetryProcessor's reckoner reference -- "
                "proof this is genuine shared state, not two coincidentally-equal copies",
            )
            self.assertEqual(
                telem_proc._reckoner.ms_since_last_known("44", ts + timedelta(seconds=2.0)),
                2000.0,
            )
        finally:
            p._db.close()


class RegistrationOrderPositionBeforeTelemetry(_Harness):
    """`SessionMessageBus.on()` dispatches same-topic handlers in
    registration order (message_bus.py) -- `dp_reckoner.py`'s own
    "IMPORTANT for WB3" docstring note and `telemetry_processor.py`'s
    last-entry `current_dp()` carve-out both depend on `PositionProcessor`
    being registered (and therefore dispatched) BEFORE `TelemetryProcessor`
    on `CarData.z`."""

    def test_position_processor_registered_before_telemetry_processor_in_processor_list(self):
        p = self._make()
        try:
            p._init_processors()
            classes = [type(x).__name__ for x in p._processors]
            pos_idx = classes.index("PositionProcessor")
            telem_idx = classes.index("TelemetryProcessor")
            self.assertLess(
                pos_idx, telem_idx,
                "PositionProcessor must be registered (and therefore subscribe()'d/"
                "dispatched) BEFORE TelemetryProcessor -- load-bearing for the "
                "batch-last-entry current_dp() carve-out",
            )
        finally:
            p._db.close()

    def test_car_data_z_handlers_fire_position_processor_before_telemetry_processor(self):
        """Not just list order -- the ACTUAL dispatch order the shared
        `SessionMessageBus` will use, confirmed by subscribing spy handlers
        alongside the real ones and observing firing order for a real
        `CarData.z` emit."""
        p = self._make()
        try:
            p._init_processors()
            call_order: list[str] = []

            pos_proc = next(x for x in p._processors if isinstance(x, PositionProcessor))
            telem_proc = p._telem_proc
            orig_pos_handler = pos_proc._handle_car_data
            orig_telem_handler = telem_proc._handle_car_data

            def _spy_pos(data, clock_time):
                call_order.append("position")
                return orig_pos_handler(data, clock_time)

            def _spy_telem(data, clock_time):
                call_order.append("telemetry")
                return orig_telem_handler(data, clock_time)

            pos_proc._handle_car_data = _spy_pos
            telem_proc._handle_car_data = _spy_telem
            # Re-subscribe so the bus's handler list points at the wrapped
            # methods (subscribe() already ran once inside _init_processors).
            p._bus._handlers["CarData.z"] = [
                _spy_pos if h == orig_pos_handler else (_spy_telem if h == orig_telem_handler else h)
                for h in p._bus._handlers["CarData.z"]
            ]

            ts = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
            p._bus.emit("CarData.z", {"Entries": [{"Cars": {"44": {"Channels": {"2": 200}}}}]}, ts)

            self.assertEqual(
                call_order, ["position", "telemetry"],
                "PositionProcessor's CarData.z handler must fire before TelemetryProcessor's",
            )
        finally:
            p._db.close()


if __name__ == "__main__":
    unittest.main()
