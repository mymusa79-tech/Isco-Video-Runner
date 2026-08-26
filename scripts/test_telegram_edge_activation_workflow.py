from __future__ import annotations

import unittest
from pathlib import Path


class TelegramEdgeActivationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = Path('.github/workflows/deploy-telegram-edge.yml').read_text(encoding='utf-8')
        self.wrangler = Path('cloudflare/telegram-control-worker/wrangler.toml.example').read_text(encoding='utf-8')

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

    def test_repository_secret_uses_non_reserved_name(self):
        self.assertIn('GITHUB_CONTROL_TOKEN: ${{ secrets.ISCO_GITHUB_CONTROL_TOKEN }}', self.text)
        self.assertNotIn('GITHUB_CONTROL_TOKEN: ${{ secrets.GITHUB_CONTROL_TOKEN }}', self.text)
        self.assertIn('put_secret GITHUB_CONTROL_TOKEN "$GITHUB_CONTROL_TOKEN"', self.text)

    def test_workers_dev_account_subdomain_is_bootstrapped_via_free_api(self):
        self.assertIn('Ensure free workers.dev account subdomain', self.text)
        self.assertIn('/workers/subdomain', self.text)
        self.assertIn('-X PUT', self.text)
        self.assertIn("hashlib.sha256(os.environ['CLOUDFLARE_ACCOUNT_ID']", self.text)
        self.assertIn('workers.dev account subdomain created in ZERO_COST_MODE', self.text)

    def test_webhook_secret_is_generated_masked_and_not_stored_in_repo(self):
        self.assertIn('Generate webhook secret', self.text)
        self.assertIn('secrets.token_urlsafe(32)', self.text)
        self.assertIn('echo "::add-mask::$WEBHOOK_SECRET"', self.text)
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

    def test_zero_cost_mode_is_explicit_and_public_repo_only(self):
        self.assertIn('ZERO_COST_MODE: "true"', self.text)
        self.assertIn('REPOSITORY_PRIVATE: ${{ github.event.repository.private }}', self.text)
        self.assertIn('refuses deployment when the Runner repository is private', self.text)
        self.assertIn('runs-on: ubuntu-latest', self.text)
        self.assertNotIn('runs-on: ubuntu-', self.text.replace('runs-on: ubuntu-latest', ''))

    def test_wrangler_is_exactly_pinned(self):
        self.assertIn('WRANGLER_VERSION: "4.125.0"', self.text)
        self.assertIn('wrangler@${WRANGLER_VERSION}', self.text)
        self.assertNotIn('wrangler@4 deploy', self.text)
        self.assertNotIn('wrangler@latest', self.text)

    def test_worker_uses_native_free_plan_limits_without_paid_limit_config(self):
        self.assertIn('workers_dev = true', self.wrangler)
        self.assertNotIn('[limits]', self.wrangler)
        self.assertNotIn('cpu_ms =', self.wrangler)
        self.assertNotIn('subrequests =', self.wrangler)
        self.assertIn('Cloudflare Workers Free', self.wrangler)
        self.assertIn('ZERO_COST_MODE = "true"', self.wrangler)
        self.assertIn("assert 'limits' not in data", self.text)

    def test_paid_or_extra_cloudflare_bindings_are_fail_closed(self):
        for key in (
            'kv_namespaces', 'd1_databases', 'r2_buckets', 'durable_objects',
            'queues', 'workflows', 'analytics_engine_datasets', 'vectorize',
            'hyperdrive', 'dispatch_namespaces',
        ):
            self.assertIn(key, self.text)
            self.assertNotIn(f'[{key}]', self.wrangler)

    def test_activation_has_no_artifact_or_paid_runner_path(self):
        self.assertNotIn('upload-artifact', self.text)
        self.assertNotIn('larger', self.text.casefold())
        self.assertNotIn('macos-', self.text)
        self.assertNotIn('windows-', self.text)


if __name__ == '__main__':
    unittest.main()
