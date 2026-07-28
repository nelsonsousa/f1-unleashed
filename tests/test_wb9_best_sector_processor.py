"""WB-9 batch 1 — BestSectorProcessor: each driver's fastest S1/S2/S3 (a
wildcard subscriber to `driverSectors:{num}`) plus its band colour vs the
session-best sector; when the session best moves, the whole field is
recoloured.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.best_sector_processor import BestSectorProcessor


def _sectors(*values):
    """Build a driverSectors:{num} payload from raw value strings (or None)."""
    return [{"value": v} if v is not None else {"value": None} for v in values]


class BestSectorProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = BestSectorProcessor(bus, "Race")
        proc.subscribe()
        vals, colours = {}, {}
        for num in ("44", "1"):
            vals[num] = []
            colours[num] = []
            bus.on(f"driverBestSectors:{num}", (lambda n: lambda d, ct: vals[n].append(list(d)))(num))
            bus.on(f"driverBestSectorColour:{num}", (lambda n: lambda d, ct: colours[n].append(list(d)))(num))
        return bus, vals, colours

    def test_first_driver_first_time_is_session_purple(self):
        bus, vals, colours = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("driverSectors:44", _sectors("25.108", "30.222", "20.001"), ct)
        self.assertEqual(vals["44"][-1], ["25.108", "30.222", "20.001"])
        self.assertEqual(colours["44"][-1], ["purple", "purple", "purple"])

    def test_faster_rival_recolours_the_field(self):
        bus, vals, colours = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("driverSectors:44", _sectors("25.108", "30.222", "20.001"), ct)
        bus.emit("driverSectors:1", _sectors("24.900", "30.222", "21.000"), ct)  # S1 faster
        # Driver 1 takes purple S1; driver 44's S1 must be recoloured off purple.
        self.assertEqual(colours["1"][-1][0], "purple")
        self.assertNotEqual(colours["44"][-1][0], "purple")

    def test_slower_time_does_not_improve_and_does_not_reemit(self):
        bus, vals, colours = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("driverSectors:44", _sectors("25.108", "30.222", "20.001"), ct)
        bus.emit("driverSectors:44", _sectors("26.000", "31.000", "21.000"), ct)  # slower
        self.assertEqual(len(vals["44"]), 1, "a slower sector must not update the personal best")

    def test_unparseable_value_is_ignored(self):
        bus, vals, colours = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("driverSectors:44", _sectors("not-a-number", None, "20.001"), ct)
        self.assertEqual(vals["44"][-1], [None, None, "20.001"])

    def test_non_matching_topic_is_ignored(self):
        bus, vals, colours = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("someOtherTopic", {"value": "1"}, ct)
        self.assertEqual(vals["44"], [])
        self.assertEqual(colours["44"], [])


if __name__ == "__main__":
    unittest.main()
