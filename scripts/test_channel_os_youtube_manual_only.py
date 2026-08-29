import tempfile
import unittest
from pathlib import Path

from scripts.channel_os_memory import AutonomyMode, ChannelOSMemory, OperationalPolicy
from scripts.channel_os_proactive_operator import ProactiveSignal
from scripts.channel_os_publication_policy import (
    YOUTUBE_UPLOAD_MODE,
    YOUTUBE_UPLOADER,
    channel_os_youtube_publish_allowed,
    channel_os_youtube_upload_allowed,
    publication_contract,
)


class YouTubeManualOnlyContractTests(unittest.TestCase):
    def test_channel_os_never_owns_youtube_upload_or_publish_in_any_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = ChannelOSMemory(tmp)
            for mode in AutonomyMode:
                policy = memory.set_policy(
                    autonomy_mode=mode,
                    auto_retry_enabled=True,
                    explicit_user_change=True,
                )
                contract = publication_contract(policy)
                self.assertEqual(contract.upload_mode, "manual_in_youtube_studio")
                self.assertEqual(contract.uploader, "user_only")
                self.assertFalse(contract.channel_os_upload_allowed)
                self.assertFalse(contract.channel_os_publish_allowed)
                self.assertFalse(channel_os_youtube_upload_allowed(policy))
                self.assertFalse(channel_os_youtube_publish_allowed(policy))

    def test_weakened_publish_firewall_fails_before_publication_contract(self):
        bad = OperationalPolicy(
            autonomy_mode="autopilot",
            auto_retry_enabled=True,
            require_publish_approval=False,
            updated_at="now",
        )
        with self.assertRaises(RuntimeError):
            publication_contract(bad)

    def test_proactive_operator_rejects_upload_and_publish_actions(self):
        common = dict(
            signal_id="s",
            level="opportunity",
            title="x",
            reason="x",
            evidence=("e1",),
            action_label="x",
            confidence=1.0,
        )
        for callback in ("cmd:youtube-upload", "cmd:youtube-publish"):
            with self.assertRaises(ValueError):
                ProactiveSignal(action_callback=callback, **common)

    def test_live_production_delivery_contract_is_manual_only(self):
        workflow = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        self.assertIn("manual_in_youtube_studio", workflow)
        self.assertIn("publication_performed", workflow)
        self.assertIn("Canonical delivery attempted to change manual YouTube publication", workflow)
        self.assertIn("Canonical delivery falsely claims publication", workflow)

    def test_policy_constants_match_live_delivery_contract(self):
        self.assertEqual(YOUTUBE_UPLOAD_MODE, "manual_in_youtube_studio")
        self.assertEqual(YOUTUBE_UPLOADER, "user_only")


if __name__ == "__main__":
    unittest.main()
