from __future__ import annotations

import unittest
from pathlib import Path


class ChannelOSTelegramJunctionSourceTests(unittest.TestCase):
    def test_poll_and_webhook_share_the_same_channel_os_installation_seam(self) -> None:
        bot_api = Path("scripts/telegram_bot_api_10_3_ui.py").read_text(encoding="utf-8")
        topic_memory = Path("scripts/telegram_topic_memory_ui.py").read_text(encoding="utf-8")
        webhook = Path("scripts/telegram_webhook_replay.py").read_text(encoding="utf-8")
        workflow = Path(".github/workflows/telegram-editorial-control.yml").read_text(encoding="utf-8")

        self.assertIn("from scripts import channel_os_telegram_adapter", bot_api)
        self.assertIn("channel_os_telegram_adapter.install()", bot_api)
        self.assertIn("bot_api_10_3_ui.install()", topic_memory)
        self.assertIn("memory_ui._install_choice_clarity()", webhook)
        self.assertIn("python scripts/telegram_topic_memory_ui.py poll", workflow)

    def test_channel_os_adapter_has_no_independent_transport_or_production_authority(self) -> None:
        adapter = Path("scripts/channel_os_telegram_adapter.py").read_text(encoding="utf-8")
        for forbidden in (
            "getUpdates",
            "TELEGRAM_BOT_TOKEN",
            "telegram_outbox_runtime",
            "enqueue_request",
            "sendMessage",
            "workflow_dispatch",
            "gh workflow run",
        ):
            self.assertNotIn(forbidden, adapter)

    def test_channel_os_callbacks_are_read_only_ui_commands(self) -> None:
        adapter = Path("scripts/channel_os_telegram_adapter.py").read_text(encoding="utf-8")
        for command in ("channelos-refresh", "channelos-needs", "channelos-problems"):
            self.assertIn(command, adapter)
        self.assertIn("client.send(chat_id, text, keyboard=keyboard)", adapter)
        self.assertNotIn("save_state", adapter)
        self.assertNotIn("production_queue", adapter)
        self.assertNotIn("release-approval-only", adapter)


if __name__ == "__main__":
    unittest.main()
