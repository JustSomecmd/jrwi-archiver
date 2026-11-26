"""Messages to the notification address in Settings: an ntfy topic or a Discord webhook."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("jrwi.notify")


def send(url: str, text: str, title: str = "JRWI archiver", post=requests.post) -> bool:
    """Send one message. True when it went out."""
    try:
        if "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url:
            r = post(url, json={"content": text[:1900]}, timeout=20)
        else:
            r = post(url, data=text.encode(), headers={"Title": title}, timeout=20)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("notification failed: %s", e)
        return False
