from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.telegram_production_queue import live_dispatch_count, mark_dispatch_failed


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "telegram-editorial-control.yml"


class TelegramGatewayDispatchCompensationTests(unittest.TestCase):
    def test_editorial_gateway_dispatch_has_exact_failure_compensation(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        required = (
            "id: dispatch_gateway",
            "steps.dispatch_gateway.outcome == 'failure'",
            "git show origin/control-plane-state:state/control-panel.json.enc",
            "python scripts/telegram_v4_ingress.py fail",
            "--reason workflow_dispatch_failed",
            'cp "$fresh_state" "$CONTROL_STATE_PATH"',
            "state: compensate failed Telegram gateway dispatch",
        )
        missing = [needle for needle in required if needle not in text]
        self.assertFalse(missing, f"Gateway dispatch compensation contract drifted: {missing}")
        self.assertLess(
            text.index("Compensate durable reservation if gateway dispatch failed"),
            text.index("Build sanitized Telegram read projection"),
        )

    def test_editorial_workflow_remains_dispatch_only(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("gh workflow run telegram-production-request.yml", text)
        self.assertNotIn("python ../scripts/run_v3_voice.py", text)
        self.assertNotIn("python ../scripts/run_telegram_control_production.py", text)

    def test_failed_reserved_authorization_is_not_live_queue_depth(self) -> None:
        state = {
            "production_queue": [
                {
                    "request_id": "req-1",
                    "request_sha256": "a" * 64,
                    "authorization_id": "b" * 32,
                    "status": "dispatch_reserved",
                    "reserved_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
        }
        self.assertEqual(live_dispatch_count(state), 1)
        item = mark_dispatch_failed(
            state,
            "req-1",
            "a" * 64,
            "b" * 32,
            reason="workflow_dispatch_failed",
        )
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["failure_reason"], "workflow_dispatch_failed")
        self.assertEqual(live_dispatch_count(state), 0)


if __name__ == "__main__":
    unittest.main()
