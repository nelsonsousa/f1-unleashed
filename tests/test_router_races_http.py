"""WB-7 — HTTP-level contract tests for app/routers/races.py.

get_event_schedule / get_session (FastF1 wrappers, app.services.f1_service)
and the F1 livetiming SessionInfo.json fetch (aiohttp) are mocked; nothing
here reaches the network.
"""
import unittest
from unittest import mock
from datetime import datetime, timezone

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
import app.routers.races as races_router


def _schedule_df():
    return pd.DataFrame([{
        "RoundNumber": 1,
        "EventName": "Bahrain Grand Prix",
        "Country": "Bahrain",
        "Location": "Sakhir",
        "EventDate": "2026-03-08",
        "EventFormat": "conventional",
        "Session1": "Practice 1", "Session1DateUtc": "2026-03-06T11:30:00",
        "Session2": "Practice 2", "Session2DateUtc": "2026-03-06T15:00:00",
        "Session3": None, "Session3DateUtc": None,
        "Session4": None, "Session4DateUtc": None,
        "Session5": "Race", "Session5DateUtc": "2099-03-08T15:00:00",
    }])


class YearsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_years_includes_current_year_down_to_2018(self):
        r = self.client.get("/api/v1/years")
        self.assertEqual(r.status_code, 200)
        years = r.json()["years"]
        self.assertEqual(years[-1], 2018)
        self.assertEqual(years[0], datetime.now().year)


class ScheduleEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_schedule_shapes_events_and_sessions(self):
        with mock.patch.object(races_router, "get_event_schedule", return_value=_schedule_df()):
            r = self.client.get("/api/v1/schedule/2026")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["year"], 2026)
        self.assertEqual(len(body["events"]), 1)
        event = body["events"][0]
        self.assertEqual(event["name"], "Bahrain Grand Prix")
        # Session3/4 are None and must be skipped, not emitted as blanks.
        session_names = [s["name"] for s in event["sessions"]]
        self.assertEqual(session_names, ["Practice 1", "Practice 2", "Race"])

    def test_schedule_upstream_failure_is_500(self):
        with mock.patch.object(races_router, "get_event_schedule", side_effect=RuntimeError("fastf1 down")):
            r = self.client.get("/api/v1/schedule/2026")
        self.assertEqual(r.status_code, 500)


class NextSessionEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_returns_next_upcoming_session(self):
        with mock.patch.object(races_router, "get_event_schedule", return_value=_schedule_df()):
            r = self.client.get("/api/v1/schedule/next-session")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["event_name"], "Bahrain Grand Prix")
        self.assertEqual(body["session_type"], "Race")

    def test_no_upcoming_sessions_is_404(self):
        empty = pd.DataFrame(columns=_schedule_df().columns)
        with mock.patch.object(races_router, "get_event_schedule", return_value=empty):
            r = self.client.get("/api/v1/schedule/next-session")
        self.assertEqual(r.status_code, 404)


class LiveSessionEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_upstream_failure_maps_to_502(self):
        class _FakeSession:
            async def __aenter__(self):
                raise RuntimeError("network down")

            async def __aexit__(self, *a):
                return False

        with mock.patch("aiohttp.ClientSession", return_value=_FakeSession()):
            r = self.client.get("/api/v1/schedule/live-session")
        self.assertEqual(r.status_code, 502)


class RaceResultsEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_returns_results_for_session(self):
        results_df = pd.DataFrame([
            {"Position": 1, "DriverNumber": "1", "Abbreviation": "VER",
             "TeamName": "Red Bull", "Status": "Finished"},
        ])

        class _FakeSession:
            results = results_df

        with mock.patch.object(races_router, "get_session", return_value=_FakeSession()):
            r = self.client.get("/api/v1/results/2025/1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["results"][0]["Abbreviation"], "VER")

    def test_missing_session_is_500(self):
        with mock.patch.object(races_router, "get_session", side_effect=ValueError("no such race")):
            r = self.client.get("/api/v1/results/2025/999")
        self.assertEqual(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
