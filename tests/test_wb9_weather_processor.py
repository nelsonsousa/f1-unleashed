"""WB-9 batch 1 — WeatherProcessor: sticky F1 deltas for track/air conditions.
Only emits when a subscribed field actually changes; unrecognised or absent
fields are carried forward from the previous state.
"""
import unittest
from datetime import datetime, timezone

from app.processing.message_bus import SessionMessageBus
from app.processing.processors.weather_processor import WeatherProcessor


class WeatherProcessorTests(unittest.TestCase):
    def _make(self):
        bus = SessionMessageBus()
        proc = WeatherProcessor(bus, "Race")
        proc.subscribe()
        got = []
        bus.on("weatherData", lambda d, ct: got.append(dict(d)))
        return bus, got

    def test_full_reading_emits_all_mapped_fields(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("WeatherData", {
            "AirTemp": "22.5", "TrackTemp": "31.0", "Pressure": "1008.1",
            "Humidity": "55", "Rainfall": "0", "WindSpeed": "1.4",
            "WindDirection": "180",
        }, ct)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], {
            "airTemp": 22.5, "trackTemp": 31.0, "pressure": 1008.1,
            "humidity": 55.0, "rain": 0.0, "windSpeed": 1.4, "windDirection": 180.0,
        })

    def test_unchanged_reading_does_not_reemit(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        msg = {"AirTemp": "22.5"}
        bus.emit("WeatherData", msg, ct)
        bus.emit("WeatherData", dict(msg), ct)   # identical reading again
        self.assertEqual(len(got), 1, "identical readings must not re-emit")

    def test_partial_update_carries_forward_previous_fields(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("WeatherData", {"AirTemp": "20.0", "TrackTemp": "28.0"}, ct)
        bus.emit("WeatherData", {"TrackTemp": "29.0"}, ct)   # only track temp changes
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["airTemp"], 20.0, "airTemp must carry forward")
        self.assertEqual(got[1]["trackTemp"], 29.0)

    def test_non_dict_payload_is_ignored(self):
        bus, got = self._make()
        ct = datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc)
        bus.emit("WeatherData", "not-a-dict", ct)
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
