from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE_FILES = (
    ROOT / "cloudflare/telegram-control-worker/index.js",
    ROOT / "cloudflare/telegram-control-worker/observability-worker-v4-core.js",
    ROOT / "cloudflare/telegram-control-worker/observability-worker.js",
)
CANONICAL_PRODUCTION_PATHS = (
    ".github/workflows/telegram-production-request.yml",
    ".github/workflows/produce-resilient-v4.yml",
)


class TelegramProductionRunIdentityTests(unittest.TestCase):
    def test_every_edge_monitor_keys_production_by_canonical_workflow_path(self) -> None:
        for path in EDGE_FILES:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for workflow_path in CANONICAL_PRODUCTION_PATHS:
                    self.assertIn(workflow_path, text)
                self.assertNotIn('name.startsWith("Produce Resilient")', text)

    def test_refresh_remains_read_only_while_confirmation_owns_production(self) -> None:
        v4 = (ROOT / "cloudflare/telegram-control-worker/observability-worker-v4-core.js").read_text(encoding="utf-8")
        base = (ROOT / "cloudflare/telegram-control-worker/index.js").read_text(encoding="utf-8")
        self.assertIn('data === "cmd:refresh_all"', v4)
        self.assertIn("قراءة فقط؛ لا يبدأ ولا يعيد أي Production Run", v4)
        self.assertIn('const CONFIRM_TEXT = "تأكيد الإنتاج"', base)


if __name__ == "__main__":
    unittest.main()
