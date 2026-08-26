from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "index.js"


class TelegramSearchScopeEdgeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKER.read_text(encoding="utf-8")

    def test_worker_javascript_syntax_when_node_is_available(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(WORKER)], check=True, capture_output=True, text=True)

    def test_edge_search_menu_has_exact_three_modes(self):
        self.assertIn('callback_data: "cmd:topic_bundle"', self.text)
        self.assertIn('callback_data: "cmd:topic_long"', self.text)
        self.assertIn('callback_data: "cmd:short"', self.text)
        self.assertIn('text: "🎬➕⚡ حلقة + Shorts"', self.text)
        self.assertIn('text: "🎬 حلقة فقط"', self.text)
        self.assertIn('text: "⚡ Short فقط"', self.text)

    def test_edge_keeps_legacy_topic_callback_compatible_but_not_primary(self):
        self.assertIn('data === "cmd:topic"', self.text)
        search_rows = self.text.split("const SEARCH_ROWS = [", 1)[1].split("];", 1)[0]
        self.assertNotIn('callback_data: "cmd:topic"', search_rows)

    def test_search_gets_persistent_progress_message_before_control_dispatch(self):
        self.assertIn("researchQueuedText", self.text)
        self.assertIn("إذا تأخر GitHub Actions سيبقى الطلب في الانتظار بدل أن يختفي بصمت", self.text)
        block = self.text.split("if (isResearchStart(target.data))", 1)[1].split("return new Response", 1)[0]
        self.assertLess(block.index("await send"), block.index("await dispatchToGitHub"))

    def test_edge_still_cannot_dispatch_production_directly(self):
        self.assertIn('const WORKFLOW = "telegram-editorial-control.yml"', self.text)
        self.assertNotIn("telegram-production-request.yml", self.text)
        self.assertNotIn("produce-resilient-v4.yml", self.text)


if __name__ == "__main__":
    unittest.main()
