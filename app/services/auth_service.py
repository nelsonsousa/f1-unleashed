import os
import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone
import urllib.parse
import webbrowser

import jwt
import requests

from app import settings

logger = logging.getLogger(__name__)

# FastF1 auth file location
AUTH_DATA_DIR = Path(os.path.expanduser("~/Library/Application Support/fastf1"))
AUTH_DATA_FILE = AUTH_DATA_DIR / "f1auth.json"


@dataclass
class AuthStatus:
    """Current authentication status."""
    is_authenticated: bool
    subscription_status: Optional[str] = None
    subscribed_product: Optional[str] = None
    expires_at: Optional[str] = None
    expires_in_hours: Optional[float] = None
    expires_in_days: Optional[float] = None
    expiring_soon: bool = False
    error: Optional[str] = None


class F1AuthService:
    """Service for managing Formula 1 authentication."""

    def __init__(self):
        self._cached_token: Optional[str] = None

    def _load_token(self) -> Optional[str]:
        """Load token from storage file."""
        if not AUTH_DATA_FILE.exists():
            return None

        try:
            content = AUTH_DATA_FILE.read_text().strip()
            if not content:
                return None

            data = json.loads(content)
            return data.get("subscription_token")
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"Failed to load auth token: {e}")
            return None

    def _save_token(self, token: str) -> None:
        """Save token to storage file."""
        AUTH_DATA_DIR.mkdir(parents=True, exist_ok=True)

        data = {"subscription_token": token}
        AUTH_DATA_FILE.write_text(json.dumps(data, indent=2))
        try:
            AUTH_DATA_FILE.chmod(0o600)   # owner-only — it's a subscription JWT (L5)
        except OSError:
            pass
        logger.info("Auth token saved successfully")

    def _decode_token(self, token: str) -> Optional[dict]:
        """Decode JWT token without verification (for reading claims)."""
        try:
            # Decode without verification to get claims
            claims = jwt.decode(token, options={"verify_signature": False})
            return claims
        except jwt.exceptions.DecodeError as e:
            logger.warning(f"Failed to decode token: {e}")
            return None

    def get_status(self) -> AuthStatus:
        """Get current authentication status."""
        token = self._load_token()

        if not token:
            return AuthStatus(
                is_authenticated=False,
                error="No authentication token found. Please log in."
            )

        claims = self._decode_token(token)
        if not claims:
            return AuthStatus(
                is_authenticated=False,
                error="Invalid token format"
            )

        # Check expiration
        exp = claims.get("exp")
        expires_at = None
        expires_in_hours = None
        expires_in_days = None
        expiring_soon = False

        # Threshold for "expiring soon" warning (default 24 hours)
        expiry_warning_hours = float(settings.get("auth.expiryWarningHours", 24.0))

        if exp:
            exp_time = datetime.fromtimestamp(exp, tz=timezone.utc)
            now = datetime.now(tz=timezone.utc)

            if exp_time < now:
                return AuthStatus(
                    is_authenticated=False,
                    error="Token has expired. Please log in again."
                )

            expires_at = exp_time.isoformat()
            time_remaining = exp_time - now
            expires_in_hours = time_remaining.total_seconds() / 3600
            expires_in_days = expires_in_hours / 24
            expiring_soon = expires_in_hours <= expiry_warning_hours

        # Check subscription status
        subscription_status = claims.get("SubscriptionStatus")
        subscribed_product = claims.get("SubscribedProduct")

        if subscription_status and subscription_status.lower() != "active":
            return AuthStatus(
                is_authenticated=False,
                subscription_status=subscription_status,
                subscribed_product=subscribed_product,
                expires_at=expires_at,
                expires_in_hours=expires_in_hours,
                expires_in_days=expires_in_days,
                expiring_soon=expiring_soon,
                error=f"Subscription is not active: {subscription_status}"
            )

        return AuthStatus(
            is_authenticated=True,
            subscription_status=subscription_status,
            subscribed_product=subscribed_product,
            expires_at=expires_at,
            expires_in_hours=expires_in_hours,
            expires_in_days=expires_in_days,
            expiring_soon=expiring_soon
        )

    def get_login_url(self) -> str:
        """Get the URL for F1 login."""
        return "https://account.formula1.com/#/en/login"

    def start_login_flow(self, open_browser: bool = True) -> dict:
        """
        Start the login flow by opening the F1 login page.

        Returns dict with:
        - login_url: URL to open for login
        - instructions: User instructions
        """
        login_url = self.get_login_url()

        if open_browser:
            webbrowser.open(login_url)

        return {
            "login_url": login_url,
            "instructions": "1. Log in to your F1 account in the browser.\n2. After login, open Developer Tools (F12).\n3. Go to Application > Cookies > account.formula1.com\n4. Find the 'login-session' cookie and copy its value.\n5. Use the /api/v1/auth/set-token endpoint to save it.",
            "status": "waiting_for_manual_token"
        }

    def set_token_from_cookie(self, cookie_value: str) -> dict:
        """
        Set the auth token from a login-session cookie value.

        The cookie value should be URL-decoded JSON containing the subscription token.
        """
        try:
            # URL decode if needed
            if '%' in cookie_value:
                cookie_value = urllib.parse.unquote(cookie_value)

            # Parse the JSON
            cookie_data = json.loads(cookie_value)

            # Extract the subscription token
            token = cookie_data.get("data", {}).get("subscriptionToken")

            if not token:
                return {"success": False, "error": "No subscription token found in cookie"}

            # Validate the token
            claims = self._decode_token(token)
            if not claims:
                return {"success": False, "error": "Invalid token format"}

            # Save it
            self._save_token(token)

            return {"success": True, "message": "Token saved successfully"}

        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid JSON in cookie: {e}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def logout(self) -> dict:
        """Clear the stored authentication token."""
        try:
            if AUTH_DATA_FILE.exists():
                AUTH_DATA_FILE.write_text("")
            self._cached_token = None
            logger.info("Logged out successfully")
            return {"success": True, "message": "Logged out successfully"}
        except IOError as e:
            logger.error(f"Failed to logout: {e}")
            return {"success": False, "error": str(e)}

    def is_authenticated(self) -> bool:
        """Quick check if user is authenticated."""
        return self.get_status().is_authenticated

    def has_credentials(self) -> bool:
        """Check if F1 credentials are configured."""
        return bool(os.getenv("F1_EMAIL")) and bool(os.getenv("F1_PASSWORD"))

    def send_expiry_notification(self, status: AuthStatus) -> bool:
        """
        Send a webhook notification about token expiry.

        Requires the ntfy webhook URL to be set in settings (card 27).
        Supports ntfy.sh, Slack, Discord, and generic webhooks.
        Returns True if notification was sent successfully.
        """
        webhook_url = settings.get("ntfy.webhookUrl")
        if not webhook_url:
            logger.debug("No webhook URL configured, skipping notification")
            return False

        try:
            if status.expires_in_hours is not None:
                hours = round(status.expires_in_hours, 1)
                if hours < 1:
                    time_str = f"{int(status.expires_in_hours * 60)} minutes"
                elif hours < 24:
                    time_str = f"{hours} hours"
                else:
                    time_str = f"{round(status.expires_in_days, 1)} days"
            else:
                time_str = "unknown time"

            message = f"Your F1 authentication token will expire in {time_str}.\n\nRun: python -m app.cli.login"

            # Detect webhook type and format accordingly
            if "ntfy.sh" in webhook_url or "ntfy." in webhook_url:
                # ntfy.sh uses headers for metadata
                headers = {
                    "Title": "F1 Auth Token Expiring",
                    "Priority": "high",
                    "Tags": "warning,formula1"
                }
                response = requests.post(webhook_url, data=message, headers=headers, timeout=10)
                response.raise_for_status()
                logger.info("Expiry notification sent successfully")
                return True
            elif "discord.com/api/webhooks" in webhook_url:
                # Discord format
                payload = {
                    "content": f"⚠️ **F1 Auth Token Expiring**\n\n{message}"
                }
            elif "hooks.slack.com" in webhook_url:
                # Slack format
                payload = {
                    "text": f"⚠️ *F1 Auth Token Expiring*\n\n{message}"
                }
            else:
                # Generic webhook format
                payload = {
                    "title": "F1 Auth Token Expiring",
                    "message": message,
                    "expires_at": status.expires_at,
                    "expires_in_hours": status.expires_in_hours,
                    "expires_in_days": status.expires_in_days
                }

            response = requests.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Expiry notification sent successfully")
            return True

        except Exception as e:
            logger.error(f"Failed to send expiry notification: {e}")
            return False

    def check_and_notify_expiry(self) -> dict:
        """
        Check token expiry and send notification if expiring soon.

        Returns status dict with notification result.
        """
        status = self.get_status()

        result = {
            "is_authenticated": status.is_authenticated,
            "expiring_soon": status.expiring_soon,
            "expires_in_hours": status.expires_in_hours,
            "expires_in_days": status.expires_in_days,
            "notification_sent": False
        }

        if not status.is_authenticated:
            result["error"] = status.error
            # Send notification for expired token too
            if "expired" in (status.error or "").lower():
                result["notification_sent"] = self.send_expiry_notification(status)
            return result

        if status.expiring_soon:
            result["notification_sent"] = self.send_expiry_notification(status)

        return result


# Global instance
auth_service = F1AuthService()
