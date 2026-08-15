"""Register the Yamtrack Telegram webhook with the Telegram Bot API."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from app.helpers import get_configured_app_url
from integrations import telegram


class Command(BaseCommand):
    """Register this instance's public URL as the Telegram bot's webhook."""

    help = "Register the Telegram bot webhook for this instance"

    def handle(self, *_args, **_options):
        """Call Telegram's setWebhook with this instance's public webhook URL."""
        if not settings.TELEGRAM_BOT_TOKEN:
            msg = "TELEGRAM_BOT_TOKEN is not configured."
            raise CommandError(msg)

        base_url = get_configured_app_url()
        if not base_url:
            msg = (
                "Could not determine this instance's public URL. "
                "Set the URLS or BASE_URL environment variable first."
            )
            raise CommandError(msg)

        webhook_url = f"{base_url}{reverse('telegram_webhook')}"

        if telegram.set_webhook(webhook_url):
            self.stdout.write(
                self.style.SUCCESS(f"Telegram webhook registered: {webhook_url}"),
            )
        else:
            msg = "Failed to register Telegram webhook. Check the logs for details."
            raise CommandError(msg)
