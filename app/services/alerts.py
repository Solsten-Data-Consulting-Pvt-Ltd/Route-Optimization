"""Alerts to the project's Google Chat space (incoming webhook).

Best-effort and never raises: an alert problem must not break routing. With no
ALERT_CHAT_WEBHOOK_URL the alert is only written to the log. The same key is
sent at most once per ALERT_COOLDOWN_S per instance, so an OSRM outage during a
busy morning is one message, not one per DRS.
"""

import logging
import time

import requests

from app import config

logger = logging.getLogger(__name__)

_last_sent = {}


def _label():
    return f"[{config.ALERT_ENV_NAME}] " if config.ALERT_ENV_NAME else ""


def send_alert(key, text, force=False):
    """Post `text` to the Chat space. Returns True if it was sent."""
    message = f"{_label()}{text}"
    now = time.monotonic()
    last = _last_sent.get(key)
    if not force and last is not None and now - last < config.ALERT_COOLDOWN_S:
        logger.info("Alert %s suppressed (cooldown): %s", key, message)
        return False
    _last_sent[key] = now
    logger.error("ALERT %s: %s", key, message)
    if not config.ALERT_CHAT_WEBHOOK_URL:
        return False
    try:
        resp = requests.post(config.ALERT_CHAT_WEBHOOK_URL, json={"text": message}, timeout=10)
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Could not send alert %s to Google Chat", key)
        return False


def reset():
    """For tests."""
    _last_sent.clear()
