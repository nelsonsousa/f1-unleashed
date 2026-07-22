"""B08 HMRnZH1V — a non-numeric QualifyingPart in a SessionData Series entry must
not raise and drop the REST of the Series: a valid segment after the bad entry
must still apply (regression for the int(qp) crash).
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.session_info_processor import SessionInfoProcessor


class B08QualifyingPart(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = SessionInfoProcessor(bus, "Qualifying")
        proc.subscribe()
        got = []
        bus.on("qualifyingPart", lambda d, ct: got.append(d))
        return bus, got

    def test_nonnumeric_qualifyingpart_does_not_drop_later_entries(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        utc = "2026-07-18T12:00:00.000Z"
        # First entry has a junk QualifyingPart; the SECOND is a valid Q2.
        bus.emit("SessionData", {"Series": [
            {"Utc": utc, "QualifyingPart": "not-a-number"},
            {"Utc": utc, "QualifyingPart": 2},
        ]}, ct)
        self.assertEqual(got, [2], "valid Q2 after a junk entry must still be emitted")

    def test_valid_qualifyingpart_still_works(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        bus.emit("SessionData", {"Series": [
            {"Utc": "2026-07-18T12:00:00.000Z", "QualifyingPart": 1},
        ]}, ct)
        self.assertEqual(got, [1])


if __name__ == "__main__":
    unittest.main()
