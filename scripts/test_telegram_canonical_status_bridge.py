from __future__ import annotations

import unittest

from scripts import telegram_canonical_status_bridge as bridge
from scripts import telegram_production_rich_ui as production_rich
from scripts import telegram_rich_integration as rich_integration


class CanonicalStatusBridgeTests(unittest.TestCase):
    def test_install_routes_legacy_renderers_to_canonical_contract(self) -> None:
        bridge.install()
        self.assertEqual(rich_integration._step_stage("Install locked Engine runtime"), "تهيئة الإنتاج")
        self.assertEqual(
            rich_integration._step_stage("Run exact approved Telegram production"),
            "الإنتاج: التخطيط → الكتابة → الصوت → المونتاج",
        )
        self.assertEqual(production_rich._stage_label("failure"), "فشل")
        self.assertEqual(production_rich._stage_label("writing"), "الكتابة")


if __name__ == "__main__":
    unittest.main()
