"""Helpers for the instance-wide Telegram bot notification integration."""

import hashlib
import hmac
import logging

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
BOT_USERNAME_CACHE_KEY = "telegram_bot_username"
BOT_USERNAME_CACHE_TIMEOUT = 60 * 60 * 24


def _webhook_secret():
    """Derive a stable webhook secret token from Django's SECRET_KEY."""
    return hashlib.sha256(
        f"{settings.SECRET_KEY}:telegram-webhook".encode(),
    ).hexdigest()


def get_bot_username():
    """Return the configured bot's username, fetched via getMe and cached."""
    if not settings.TELEGRAM_BOT_TOKEN:
        return None

    cached = cache.get(BOT_USERNAME_CACHE_KEY)
    if cached:
        return cached

    try:
        response = requests.get(
            f"{API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/getMe",
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        username = response.json()["result"]["username"]
    except (requests.RequestException, KeyError, ValueError):
        logger.exception("Failed to resolve Telegram bot username")
        return None

    cache.set(BOT_USERNAME_CACHE_KEY, username, timeout=BOT_USERNAME_CACHE_TIMEOUT)
    return username


def set_webhook(url):
    """Register the given URL as the bot's webhook. Returns True on success."""
    try:
        response = requests.post(
            f"{API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": url, "secret_token": _webhook_secret()},
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        logger.exception("Failed to register Telegram webhook")
        return False

    if not payload.get("ok"):
        logger.error("Telegram rejected webhook registration: %s", payload)
        return False

    return True


def verify_webhook_secret(header_value):
    """Return True if the given header value matches the expected webhook secret."""
    if not header_value:
        return False
    return hmac.compare_digest(header_value, _webhook_secret())


def send_message(chat_id, text):
    """Send a plain text message to the given chat ID."""
    try:
        response = requests.post(
            f"{API_BASE}/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=settings.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to send Telegram message to chat %s", chat_id)
