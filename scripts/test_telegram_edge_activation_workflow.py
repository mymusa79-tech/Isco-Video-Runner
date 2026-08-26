from __future__ import annotations

import unittest
from pathlib import Path


class TelegramEdgeActivationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path('.github/workflows/deploy-telegram-edge.yml').read_text(encoding='utf-8')

    def test_pull_requests_validate_only_and_never_deploy(self):
        self.assertIn("if: github.event_name == 'pull_request'", self.text)
        self.assertIn("if: github.event_name != 'pull_request'", self.text)
        self.assertIn('node --check cloudflare/telegram-control-worker/index.js', self.text)

    def test_activation_requires_identity_and_cloudflare_secrets(self):
        for name in (
            'CLOUDFLARE_API_TOKEN',
            'CLOUDFLARE_ACCOUNT_ID',
            'TELEGRAM_BOT_TOKEN',
            'TELEGRAM_ALLOWED_USER_ID',
            'TELEGRAM_CHAT_ID',
            'GITHUB_CONTROL_TOKEN',
            'YOUTUBE_API_KEY',
        ):
            self.assertIn(name, self.text)

    def test_webhook_secret_is_generated_not_stored_in_repo(self):
        self.assertIn('Generate webhook secret', self.text)
        self.assertIn('secrets.token_urlsafe(32)', self.text)
        self.assertIn('put_secret TELEGRAM_WEBHOOK_SECRET', self.text)
        self.assertIn('"secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"]', self.text)
        self.assertNotIn('secrets.TELEGRAM_WEBHOOK_SECRET', self.text)

    def test_activation_registers_and_verifies_webhook(self):
        self.assertIn('/setWebhook', self.text)
        self.assertIn('/getWebhookInfo', self.text)
        self.assertIn('/health', self.text)
        self.assertIn('"allowed_updates": ["message", "callback_query"]', self.text)
        self.assertIn('"drop_pending_updates": False', self.text)

    def test_activation_does_not_dispatch_production(self):
        self.assertNotIn('gh workflow run telegram-production-request.yml', self.text)
        self.assertNotIn('produce-resilient-v4.yml', self.text)
        self.assertNotIn('run_control_production.py', self.text)
        self.assertIn('Production authority remains in telegram-editorial-control.yml', self.text)

    def test_permissions_are_read_only(self):
        self.assertIn('permissions:\n  contents: read', self.text)
        self.assertNotIn('actions: write', self.text)
        self.assertNotIn('contents: write', self.text)


if __name__ == '__main__':
    unittest.main()
