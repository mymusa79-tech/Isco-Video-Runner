from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / "cloudflare/telegram-control-worker/observability-worker.js"
V5_CORE = ROOT / "cloudflare/telegram-control-worker/observability-worker-v5-core.js"
BASE = ROOT / "cloudflare/telegram-control-worker/index.js"
CANONICAL_PRODUCTION_PATHS = (
    ".github/workflows/telegram-production-request.yml",
    ".github/workflows/produce-resilient-v4.yml",
)


class TelegramProductionRunIdentityTests(unittest.TestCase):
    def test_deployed_observability_authority_keys_production_by_workflow_path(self) -> None:
        text = AUTHORITY.read_text(encoding="utf-8")
        for workflow_path in CANONICAL_PRODUCTION_PATHS:
            self.assertIn(workflow_path, text)
        self.assertIn("CANONICAL_PRODUCTION_PATHS.has(canonicalRunPath(run))", text)
        self.assertNotIn('name.startsWith("Produce Resilient")', text)

    def test_authority_intercepts_all_user_visible_production_monitor_routes(self) -> None:
        text = AUTHORITY.read_text(encoding="utf-8")
        self.assertIn('data === "cmd:status"', text)
        self.assertIn('data === "cmd:system_status"', text)
        self.assertIn('data === "cmd:refresh_all"', text)
        self.assertIn('["الحالة", "حالة", "status", "4", "٤"]', text)
        self.assertIn('import priorWorker from "./observability-worker-v5-core.js"', text)
        self.assertTrue(V5_CORE.exists())

    def test_refresh_remains_read_only_while_confirmation_owns_production(self) -> None:
        authority = AUTHORITY.read_text(encoding="utf-8")
        base = BASE.read_text(encoding="utf-8")
        self.assertIn("«تحديث الكل» قراءة فقط", authority)
        self.assertIn("التشغيل يبدأ فقط بعد «تأكيد الإنتاج»", authority)
        self.assertIn('const CONFIRM_TEXT = "تأكيد الإنتاج"', base)

    def test_live_v4_is_preferred_over_gateway_when_both_are_active(self) -> None:
        text = AUTHORITY.read_text(encoding="utf-8")
        self.assertIn("active.find(isV4Run) || active[0]", text)
        self.assertIn('canonicalRunPath(run) === ".github/workflows/produce-resilient-v4.yml"', text)


if __name__ == "__main__":
    unittest.main()
