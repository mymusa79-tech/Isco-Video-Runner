from __future__ import annotations

import unittest

from scripts import telegram_rich_integration as integration


class TelegramRichIntegrationTests(unittest.TestCase):
    def test_status_payload_prefers_state_and_fills_from_target(self):
        class Active:
            pass

        old = getattr(integration.active, "PRODUCTION_TARGET_KEY", None)
        integration.active.PRODUCTION_TARGET_KEY = "production_target"
        try:
            payload = integration._status_payload(
                {"stage": "planning", "production_target": {"request_id": "req-1", "title": "موضوع"}},
                [],
            )
        finally:
            if old is None:
                delattr(integration.active, "PRODUCTION_TARGET_KEY")
            else:
                integration.active.PRODUCTION_TARGET_KEY = old
        self.assertEqual(payload["stage"], "planning")
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["title"], "موضوع")

    def test_quality_payload_reads_list_from_state(self):
        payload = integration._quality_payload({"quality_gates": [{"name": "A", "passed": True}]}, [])
        self.assertEqual(payload, {"gates": [{"name": "A", "passed": True}]})


if __name__ == "__main__":
    unittest.main()
