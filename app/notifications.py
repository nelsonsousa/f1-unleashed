"""Outbound dev/ops notifications via a configured webhook.

Reads NOTIFICATION_WEBHOOK_URL from the environment (see .env / .env.example)
and posts to ntfy / Discord / Slack / generic JSON depending on the URL.
No-op (returns False) when no webhook is configured.
"""

import logging

logger = logging.getLogger(__name__)


def send_notification(
    title: str,
    message: str,
    priority: str = "default",
    tags: str = "formula1",
) -> bool:
    """Send a notification via the configured webhook. Returns success."""
    import requests

    from app import settings
    webhook_url = settings.get("ntfy.webhookUrl")
    if not webhook_url:
        return False

    try:
        if "ntfy.sh" in webhook_url or "ntfy." in webhook_url:
            headers = {"Title": title, "Priority": priority, "Tags": tags}
            response = requests.post(webhook_url, data=message, headers=headers, timeout=10)
        elif "discord.com/api/webhooks" in webhook_url:
            payload = {"content": f"**{title}**\n\n{message}"}
            response = requests.post(webhook_url, json=payload, timeout=10)
        elif "hooks.slack.com" in webhook_url:
            payload = {"text": f"*{title}*\n\n{message}"}
            response = requests.post(webhook_url, json=payload, timeout=10)
        else:
            payload = {"title": title, "message": message}
            response = requests.post(webhook_url, json=payload, timeout=10)

        response.raise_for_status()
        logger.info(f"Notification sent: {title}")
        return True
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False
