from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "editorial-worker-v7.js"
WRANGLER = ROOT / "cloudflare" / "telegram-control-worker" / "wrangler.toml.example"


class TelegramEdgeLibraryV7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKER.read_text(encoding="utf-8")
        cls.wrangler = WRANGLER.read_text(encoding="utf-8")

    def test_worker_has_valid_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(WORKER)], check=True, capture_output=True, text=True)

    def test_v7_wraps_v6_without_replacing_production_authority(self):
        self.assertIn('import priorWorker from "./observability-worker-v6.js"', self.text)
        self.assertIn("return priorWorker.fetch(request, env, ctx)", self.text)
        self.assertNotIn("produce-resilient-v4.yml", self.text)
        self.assertNotIn("telegram-production-request.yml", self.text)
        self.assertNotIn("dispatchToGitHub", self.text)

    def test_library_reads_require_webhook_and_operator_identity_boundaries(self):
        self.assertIn("X-Telegram-Bot-Api-Secret-Token", self.text)
        self.assertIn("TELEGRAM_WEBHOOK_SECRET", self.text)
        self.assertIn("TELEGRAM_ALLOWED_USER_ID", self.text)
        self.assertIn("TELEGRAM_CHAT_ID", self.text)
        self.assertIn("secretHeaderValid(request, env)", self.text)
        self.assertIn("authorized(update, env)", self.text)

    def test_saved_and_used_pages_are_edge_local(self):
        for callback in (
            'value === "cmd:saved"',
            'value === "cmd:used"',
            "cmd:saved-(long|short)",
            "cmd:used-(long|short)",
        ):
            self.assertIn(callback, self.text)
        self.assertIn("showSavedMenu(env", self.text)
        self.assertIn("showSavedPage(env", self.text)
        self.assertIn("showUsedMenu(env", self.text)
        self.assertIn("showUsedPage(env", self.text)

    def test_saved_pick_remains_stateful_and_is_not_executed_at_edge(self):
        self.assertIn("cmd:savedpick-", self.text)
        self.assertNotIn('value.startsWith("cmd:savedpick-")', self.text)
        self.assertNotIn("enqueue", self.text.casefold())
        self.assertNotIn("production_target", self.text)

    def test_edge_reads_existing_encrypted_authoritative_state_with_short_cache(self):
        self.assertIn("STATE_ENCRYPTION_KEY", self.text)
        self.assertIn("control-plane-state/state/control-panel.json.enc", self.text)
        self.assertIn("STATE_TTL_MS = 15_000", self.text)
        self.assertIn("PBKDF2", self.text)
        self.assertIn("AES-CBC", self.text)

    def test_no_new_paid_cloudflare_storage_or_service_is_required(self):
        self.assertIn('main = "editorial-worker-v7.js"', self.wrangler)
        for marker in ("KVNamespace", "D1Database", "R2Bucket", "DurableObject", "Queue"):
            self.assertNotIn(marker, self.text)


if __name__ == "__main__":
    unittest.main()
