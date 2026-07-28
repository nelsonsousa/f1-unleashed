"""WB-7 — HTTP-level contract tests for app/routers/auth.py.

Uses FastAPI's TestClient (httpx) against the real `app` instance from
app.main. Everything that would hit F1's real auth service is mocked via
unittest.mock (auth_service is a module-level singleton imported into the
router, so we patch it there — matches the project's own mocking style,
e.g. tests/test_settings_save.py mocking settings_store attributes).
"""
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from app.main import app
import app.routers.auth as auth_router
from app.services.auth_service import AuthStatus


class AuthStatusEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_status_returns_authenticated_details(self):
        status = AuthStatus(
            is_authenticated=True,
            subscription_status="active",
            subscribed_product="F1TV Pro",
            expires_at="2026-08-01T00:00:00Z",
            expires_in_hours=120.0,
            expires_in_days=5.0,
            expiring_soon=False,
        )
        with mock.patch.object(auth_router.auth_service, "get_status", return_value=status):
            r = self.client.get("/api/v1/auth/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["is_authenticated"])
        self.assertEqual(body["subscription_status"], "active")
        self.assertEqual(body["expires_in_hours"], 120.0)

    def test_status_reports_not_authenticated(self):
        status = AuthStatus(is_authenticated=False, error="No authentication token found. Please log in.")
        with mock.patch.object(auth_router.auth_service, "get_status", return_value=status):
            r = self.client.get("/api/v1/auth/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["is_authenticated"])
        self.assertIn("log in", body["error"])


class LoginFlowEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_start_login_returns_login_url_and_instructions(self):
        result = {
            "login_url": "https://account.formula1.com/#/en/login",
            "instructions": "do the thing",
            "status": "waiting_for_manual_token",
        }
        with mock.patch.object(auth_router.auth_service, "start_login_flow", return_value=result) as m:
            r = self.client.post("/api/v1/auth/login", params={"open_browser": "false"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["login_url"], result["login_url"])
        m.assert_called_once_with(open_browser=False)

    def test_get_login_url_does_not_open_browser(self):
        with mock.patch.object(auth_router.auth_service, "get_login_url",
                                return_value="https://account.formula1.com/#/en/login"):
            r = self.client.get("/api/v1/auth/login-url")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["login_url"], "https://account.formula1.com/#/en/login")

    def test_set_token_success(self):
        with mock.patch.object(auth_router.auth_service, "set_token_from_cookie",
                                return_value={"success": True, "message": "Token saved successfully"}):
            r = self.client.post("/api/v1/auth/set-token", json={"cookie_value": "abc"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_set_token_with_malformed_cookie_reports_failure_not_500(self):
        # Negative case: a bad cookie is a *handled* failure (200 + success:false),
        # not a server error — the router just passes the service dict through.
        with mock.patch.object(auth_router.auth_service, "set_token_from_cookie",
                                return_value={"success": False, "error": "Invalid JSON in cookie: ..."}):
            r = self.client.post("/api/v1/auth/set-token", json={"cookie_value": "not-json"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["success"])
        self.assertIn("error", body)

    def test_set_token_missing_body_field_is_422(self):
        r = self.client.post("/api/v1/auth/set-token", json={})
        self.assertEqual(r.status_code, 422)


class MiscAuthEndpoints(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_has_credentials_true(self):
        with mock.patch.object(auth_router.auth_service, "has_credentials", return_value=True):
            r = self.client.get("/api/v1/auth/has-credentials")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"has_credentials": True})

    def test_has_credentials_false(self):
        with mock.patch.object(auth_router.auth_service, "has_credentials", return_value=False):
            r = self.client.get("/api/v1/auth/has-credentials")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"has_credentials": False})

    def test_logout_success(self):
        with mock.patch.object(auth_router.auth_service, "logout",
                                return_value={"success": True, "message": "Logged out successfully"}):
            r = self.client.post("/api/v1/auth/logout")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["success"])

    def test_quick_check_reflects_service(self):
        with mock.patch.object(auth_router.auth_service, "is_authenticated", return_value=True):
            r = self.client.get("/api/v1/auth/check")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"authenticated": True})

    def test_check_expiry_passes_through_service_result(self):
        result = {"expiring_soon": True, "notified": True}
        with mock.patch.object(auth_router.auth_service, "check_and_notify_expiry", return_value=result):
            r = self.client.get("/api/v1/auth/check-expiry")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), result)


if __name__ == "__main__":
    unittest.main()
