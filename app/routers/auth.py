import subprocess
import sys

from fastapi import APIRouter, Query
from pydantic import BaseModel
from typing import Optional

from app.services.auth_service import auth_service

router = APIRouter()


class AuthStatusResponse(BaseModel):
    is_authenticated: bool
    subscription_status: Optional[str] = None
    subscribed_product: Optional[str] = None
    expires_at: Optional[str] = None
    expires_in_hours: Optional[float] = None
    expires_in_days: Optional[float] = None
    expiring_soon: bool = False
    error: Optional[str] = None


class LoginResponse(BaseModel):
    login_url: str
    instructions: str
    status: str


class SetTokenRequest(BaseModel):
    cookie_value: str


class SetTokenResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


class LogoutResponse(BaseModel):
    success: bool
    message: Optional[str] = None
    error: Optional[str] = None


@router.get("/status", response_model=AuthStatusResponse)
def get_auth_status():
    """
    Check the current F1 authentication status.

    Returns whether the user is logged in with a valid F1 subscription,
    including token expiration details.
    """
    status = auth_service.get_status()
    return AuthStatusResponse(
        is_authenticated=status.is_authenticated,
        subscription_status=status.subscription_status,
        subscribed_product=status.subscribed_product,
        expires_at=status.expires_at,
        expires_in_hours=round(status.expires_in_hours, 2) if status.expires_in_hours else None,
        expires_in_days=round(status.expires_in_days, 2) if status.expires_in_days else None,
        expiring_soon=status.expiring_soon,
        error=status.error
    )


@router.post("/login", response_model=LoginResponse)
def start_login(
    open_browser: bool = Query(True, description="Whether to automatically open the browser")
):
    """
    Start the F1 login flow.

    This initiates a browser-based login to formula1.com. The user must have
    an active F1 TV subscription to access live timing data.

    Returns the login URL and instructions for completing authentication.
    """
    result = auth_service.start_login_flow(open_browser=open_browser)
    return LoginResponse(**result)


@router.get("/login-url")
def get_login_url():
    """
    Get the F1 login URL without opening the browser.
    """
    return {
        "login_url": auth_service.get_login_url(),
        "instructions": "Open this URL to log in to F1. After login, copy the 'login-session' cookie from Developer Tools."
    }


@router.post("/set-token", response_model=SetTokenResponse)
def set_token(request: SetTokenRequest):
    """
    Set the auth token from a login-session cookie value.

    After logging in to formula1.com:
    1. Open Developer Tools (F12)
    2. Go to Application > Cookies > account.formula1.com
    3. Find the 'login-session' cookie
    4. Copy the entire value and paste it here
    """
    result = auth_service.set_token_from_cookie(request.cookie_value)
    return SetTokenResponse(**result)


@router.post("/browser-login")
def browser_login():
    """
    Open a browser window for F1 login.

    Spawns the CLI login script which opens a webview for authentication.
    The token is automatically captured when the user logs in.
    """
    try:
        # Spawn the login script as a subprocess (non-blocking)
        subprocess.Popen(
            [sys.executable, "-m", "app.cli.login"],
            start_new_session=True
        )
        return {"success": True, "message": "Login window opened"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/has-credentials")
def has_credentials():
    """Check if F1 credentials are configured in environment."""
    return {"has_credentials": auth_service.has_credentials()}


@router.post("/logout", response_model=LogoutResponse)
def logout():
    """
    Log out from F1 and clear stored credentials.
    """
    result = auth_service.logout()
    return LogoutResponse(**result)


@router.get("/check")
def quick_auth_check():
    """
    Quick check if authenticated (lightweight endpoint for frequent polling).
    """
    return {"authenticated": auth_service.is_authenticated()}


@router.get("/check-expiry")
def check_token_expiry():
    """
    Check if token is expiring soon and optionally send notification.

    If a webhook is configured in settings (`ntfy.webhookUrl`), sends a
    notification when the token is expiring within the configured warning
    window (`auth.expiryWarningHours`, default 24 h).

    Returns expiry status and whether notification was sent.
    """
    return auth_service.check_and_notify_expiry()
