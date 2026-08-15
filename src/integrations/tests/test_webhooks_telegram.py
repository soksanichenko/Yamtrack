import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from integrations import telegram

WEBHOOK_SECRET = telegram._webhook_secret()


@override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")  # noqa: S106
class TelegramWebhookTests(TestCase):
    """Tests for the Telegram bot webhook."""

    def setUp(self):
        """Set up test data."""
        self.client = Client()
        self.credentials = {"username": "testuser", "password": "12345"}
        self.user = get_user_model().objects.create_user(**self.credentials)
        self.url = reverse("telegram_webhook")

    def post_update(self, payload, secret=WEBHOOK_SECRET):
        """POST a Telegram update payload with the given webhook secret header."""
        headers = {}
        if secret is not None:
            headers["X-Telegram-Bot-Api-Secret-Token"] = secret
        return self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
            headers=headers,
        )

    def test_invalid_secret_token(self):
        """Test webhook with an invalid secret token returns 403."""
        response = self.post_update({}, secret="wrong-secret")  # noqa: S106
        self.assertEqual(response.status_code, 403)

    def test_missing_secret_token(self):
        """Test webhook with no secret token header returns 403."""
        response = self.post_update({}, secret=None)
        self.assertEqual(response.status_code, 403)

    def test_unknown_linking_code(self):
        """Test /start with an unknown or expired code is a no-op."""
        payload = {
            "message": {
                "text": "/start does-not-exist",
                "chat": {"id": 99999},
            },
        }
        response = self.post_update(payload)
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_chat_id, "")

    @patch("integrations.telegram.send_message")
    def test_successful_link(self, mock_send_message):
        """Test /start with a valid code links the account and clears the code."""
        cache.set("telegram_link:valid-code", self.user.id, timeout=300)

        payload = {
            "message": {
                "text": "/start valid-code",
                "chat": {"id": 555444333},
            },
        }
        response = self.post_update(payload)
        self.assertEqual(response.status_code, 200)

        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_chat_id, "555444333")
        self.assertIsNone(cache.get("telegram_link:valid-code"))
        mock_send_message.assert_called_once()

    def test_ignores_non_start_messages(self):
        """Test messages other than /start are ignored without error."""
        payload = {"message": {"text": "hello", "chat": {"id": 1}}}
        response = self.post_update(payload)
        self.assertEqual(response.status_code, 200)

    def test_ignores_non_message_updates(self):
        """Test updates without a message (e.g. edited_message) don't error."""
        response = self.post_update({"edited_message": {"text": "/start x"}})
        self.assertEqual(response.status_code, 200)
