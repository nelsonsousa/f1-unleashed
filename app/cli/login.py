#!/usr/bin/env python3
"""
F1 Login CLI - Opens a native WebView for F1 authentication.

Usage:
    python -m app.cli.login

This opens a WebView window where you can log in to your F1 account.
Once logged in, the token is automatically captured and saved.
The token can then be used by the server for ~72 hours.
"""

import json
import sys
import time
from pathlib import Path

# Auth file location (same as FastF1)
AUTH_DATA_DIR = Path.home() / "Library/Application Support/fastf1"
AUTH_DATA_FILE = AUTH_DATA_DIR / "f1auth.json"


class F1LoginHandler:
    """Handles the F1 login WebView and token capture."""

    def __init__(self):
        self.token = None
        self.window = None

    def on_cookie_received(self, cookie_value: str):
        """Called from JavaScript when login-session cookie is detected."""
        print(f"Token received! Length: {len(cookie_value)}")
        self.token = cookie_value
        self.save_token(cookie_value)

        # Close the window after a short delay
        if self.window:
            time.sleep(1)
            self.window.destroy()

    def save_token(self, token: str):
        """Save the token to the auth file."""
        try:
            # Parse the cookie JSON to extract the subscription token
            import urllib.parse
            if '%' in token:
                token = urllib.parse.unquote(token)

            cookie_data = json.loads(token)
            subscription_token = cookie_data.get("data", {}).get("subscriptionToken")

            if subscription_token:
                AUTH_DATA_DIR.mkdir(parents=True, exist_ok=True)
                data = {"subscription_token": subscription_token}
                AUTH_DATA_FILE.write_text(json.dumps(data, indent=2))
                print(f"Token saved to: {AUTH_DATA_FILE}")
                print("You can now use the F1 Archive app with authentication!")
            else:
                print("Warning: No subscription token found in cookie")
                print("Raw cookie saved for debugging")
                AUTH_DATA_DIR.mkdir(parents=True, exist_ok=True)
                AUTH_DATA_FILE.write_text(json.dumps({"raw_cookie": token}, indent=2))
        except json.JSONDecodeError as e:
            print(f"Error parsing cookie JSON: {e}")
            # Save raw cookie anyway
            AUTH_DATA_DIR.mkdir(parents=True, exist_ok=True)
            AUTH_DATA_FILE.write_text(json.dumps({"raw_cookie": token}, indent=2))
        except Exception as e:
            print(f"Error saving token: {e}")

    def run(self):
        """Open the WebView and start the login flow."""
        try:
            import webview
        except ImportError:
            print("Error: pywebview not installed")
            print("Install it with: pip install pywebview")
            sys.exit(1)

        # JavaScript to inject - polls for login-session cookie
        js_code = """
        function getCookie(name) {
            return (name = (document.cookie + ';').match(new RegExp(name + '=.*;'))) && name[0].split(/=|;/)[1];
        }

        var previousCookie = "";
        var checkInterval = setInterval(() => {
            let cookie = getCookie('login-session');
            if (cookie && previousCookie !== cookie) {
                previousCookie = cookie;
                // Call Python handler
                pywebview.api.on_cookie_received(cookie);

                // Show success message
                document.body.insertAdjacentHTML('afterbegin',
                    '<div style="background: #00d700; color: black; padding: 20px; text-align: center; font-size: 18px; font-weight: bold;">' +
                    'Login Complete! You can close this window.</div>'
                );
                clearInterval(checkInterval);
            }
        }, 1000);
        """

        print("Opening F1 login page...")
        print("Please log in with your F1 account credentials.")
        print("")

        # Create and run the WebView
        self.window = webview.create_window(
            "Login to Formula 1",
            "https://account.formula1.com/#/en/login",
            width=1024,
            height=768,
            js_api=self,
        )

        # Start WebView and inject JS after page loads
        def on_loaded():
            if self.window:
                self.window.evaluate_js(js_code)

        self.window.events.loaded += on_loaded

        # Run the WebView (blocks until closed)
        webview.start()

        if self.token:
            print("\nLogin successful!")
            return True
        else:
            print("\nLogin cancelled or failed.")
            return False


def main():
    """Main entry point for the login CLI."""
    print("=" * 50)
    print("F1 Archive - Login")
    print("=" * 50)
    print("")

    handler = F1LoginHandler()
    success = handler.run()

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
