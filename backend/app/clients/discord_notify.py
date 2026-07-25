"""Fire-and-forget Discord webhook notifier. Free, no infra: the user pastes a
channel webhook URL into Settings and new-recommendation alerts land on their
phone via the Discord app. No-op (returns False) when no URL is configured, so
the whole alert path is safely inert until the user opts in."""
import logging

import httpx

log = logging.getLogger("discord_notify")

_MAX = 1950  # Discord hard-caps message content at 2000 chars


def send_discord(webhook_url: str | None, content: str) -> bool:
    """POST a plain-text message to a Discord channel webhook. True on success."""
    if not webhook_url or not content:
        return False
    try:
        r = httpx.post(webhook_url, json={"content": content[:_MAX]}, timeout=10.0)
        if r.status_code >= 300:
            log.warning("discord webhook returned %s", r.status_code)
            return False
        return True
    except Exception:
        log.exception("discord webhook post failed")
        return False
