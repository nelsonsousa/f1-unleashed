"""WB-7 — HTTP-level contract tests for app/routers/weather.py.

Anything that would hit a real external service (Open-Meteo, the livetiming
cache on disk) is mocked; the pure-math helpers (composite_geometry,
usage_status, TRACK_LOCATIONS) are exercised for real since they have no
external dependency.
"""
import unittest
from unittest import mock

import httpx as httpx_lib
from fastapi.testclient import TestClient

from app.main import app
import app.routers.weather as weather_router


class GetWeatherEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        weather_router._cache.clear()

    def test_invalid_date_format_is_400(self):
        r = self.client.get(
            "/api/v1/weather",
            params={"latitude": 26.03, "longitude": 50.51, "date": "27-07-2026"},
        )
        self.assertEqual(r.status_code, 400)

    def test_fetches_and_shapes_hourly_data(self):
        upstream_payload = {
            "hourly": {
                "time": ["2026-07-27T00:00"],
                "temperature_2m": [28.4],
                "windspeed_10m": [12.0],
                "winddirection_10m": [180],
                "cloudcover": [10],
                "precipitation": [0.0],
                "weather_code": [1],
                "is_day": [1],
            }
        }
        fake_response = httpx_lib.Response(200, json=upstream_payload,
                                            request=httpx_lib.Request("GET", "https://x"))

        async def fake_get(self, url, params=None):
            return fake_response

        with mock.patch.object(httpx_lib.AsyncClient, "get", fake_get):
            r = self.client.get(
                "/api/v1/weather",
                params={"latitude": 26.0325, "longitude": 50.5106, "date": "2026-07-20"},
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["date"], "2026-07-20")
        self.assertEqual(body["hourly"]["temperature_2m"], [28.4])

    def test_upstream_error_maps_to_502(self):
        async def fake_get(self, url, params=None):
            raise httpx_lib.RequestError("connection refused")

        with mock.patch.object(httpx_lib.AsyncClient, "get", fake_get):
            r = self.client.get(
                "/api/v1/weather",
                params={"latitude": 26.0325, "longitude": 50.5106, "date": "2026-07-21"},
            )
        self.assertEqual(r.status_code, 502)


class RadarLatestEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_layer_is_400(self):
        r = self.client.get(
            "/api/v1/weather/radar/latest",
            params={"session": "2026_Spa_Race", "layer": "nonsense"},
        )
        self.assertEqual(r.status_code, 400)

    def test_unknown_session_returns_204(self):
        with mock.patch(
            "app.services.livetiming_fetcher.livetiming_fetcher.find_cached_session_path",
            return_value=None,
        ):
            r = self.client.get(
                "/api/v1/weather/radar/latest",
                params={"session": "does-not-exist"},
            )
        self.assertEqual(r.status_code, 204)


class ForecastEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_session_returns_204(self):
        with mock.patch(
            "app.services.livetiming_fetcher.livetiming_fetcher.find_cached_session_path",
            return_value=None,
        ):
            r = self.client.get(
                "/api/v1/weather/forecast",
                params={"session": "does-not-exist"},
            )
        self.assertEqual(r.status_code, 204)


class RadarDiagnosticsEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_radar_status_reports_inactive_by_default(self):
        r = self.client.get("/api/v1/weather/radar/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn("active", r.json())

    def test_radar_usage_reports_month_and_remaining(self):
        r = self.client.get("/api/v1/weather/radar/usage")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("remaining", body)
        self.assertIn("limit", body)


class RadarExtentEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_unknown_circuit_is_404(self):
        r = self.client.get(
            "/api/v1/weather/radar/extent",
            params={"event_name": "Not A Real Grand Prix"},
        )
        self.assertEqual(r.status_code, 404)

    def test_known_circuit_returns_geometry(self):
        r = self.client.get(
            "/api/v1/weather/radar/extent",
            params={"event_name": "Bahrain Grand Prix"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertAlmostEqual(body["lat"], 26.0325)
        self.assertIn("tile_zoom", body)
        self.assertIn("circuit_frac_x", body)


if __name__ == "__main__":
    unittest.main()
