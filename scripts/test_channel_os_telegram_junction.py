from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.channel_os_mission_control import MISSION_STATES, MissionItem, MissionSnapshot
from scripts.channel_os_publication_policy import (
    channel_os_youtube_publish_allowed,
    channel_os_youtube_upload_allowed,
)
from scripts.channel_os_memory import ChannelOSMemory
from scripts.channel_os_telegram_junction import (
    ChannelOSTelegramCommand,
    ChannelOSTelegramContractError,
    build_l6_outbox_intent,
    is_channel_os_callback,
    parse_channel_os_callback,
)
from scripts.orchestration_telegram_ingress_outbox import OutboxStatus
from scripts.telegram_outbox_runtime import enqueue

UTC = "2026-08-30T00:00:00+00:00"


def snapshot(*items: MissionItem) -> MissionSnapshot:
    counts = {state: 0 for state in MISSION_STATES}
    for item in items:
        counts[item.mission_state] += 1
    return MissionSnapshot(tuple(items), counts, UTC, sum(1 for item in items if item.source == "live-source-unavailable"))


def item(video_id: str, state: str, *, source: str = "github-actions", reason: str = "") -> MissionItem:
    return MissionItem(video_id, f"Title {video_id}", state, "77", source, UTC, reason)


class ChannelOSTelegramJunctionTests(unittest.TestCase):
    def test_callback_namespace_is_exact_and_closed(self):
        expected = {
            "cmd:channelos-refresh": ChannelOSTelegramCommand.REFRESH,
            "cmd:channelos-needs": ChannelOSTelegramCommand.NEEDS_ME,
            "cmd:channelos-problems": ChannelOSTelegramCommand.PROBLEMS,
        }
        for callback, command in expected.items():
            self.assertTrue(is_channel_os_callback(callback))
            self.assertEqual(parse_channel_os_callback(callback), command)
        for invalid in ("", "cmd:channelos", "cmd:channelos-publish", "cmd:produce_latest", "channelos-refresh"):
            self.assertFalse(is_channel_os_callback(invalid))
            with self.assertRaises(ChannelOSTelegramContractError):
                parse_channel_os_callback(invalid)

    def test_refresh_maps_snapshot_to_l6_outbox_intent_without_transport_authority(self):
        current = snapshot(item("a", "Producing"), item("b", "Needs Me"), item("c", "Problems"))
        intent = build_l6_outbox_intent(
            current,
            command=ChannelOSTelegramCommand.REFRESH,
            interaction_id="update-100",
        )
        self.assertEqual(intent["schema_version"], 1)
        self.assertEqual(intent["method"], "sendMessage")
        self.assertEqual(intent["message_kind"], "channel_os_mission_control")
        self.assertEqual(intent["authority"], "projection_only")
        self.assertEqual(intent["transport_owner"], "l6_telegram_outbox")
        self.assertEqual(intent["publication_authority"], "none")
        self.assertTrue(str(intent["journal_event_ref"]).startswith("projection:channel-os:"))
        payload = intent["payload"]
        self.assertIn("Isco Channel OS", payload["text"])
        self.assertIn("cmd:channelos-refresh", str(payload["reply_markup"]))

    def test_needs_and_problems_callbacks_filter_only_requested_mission_state(self):
        current = snapshot(
            item("a", "Ready"),
            item("b", "Needs Me", reason="approval"),
            item("c", "Problems", source="live-source-unavailable", reason="offline"),
        )
        needs = build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.NEEDS_ME, interaction_id="101")
        problems = build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.PROBLEMS, interaction_id="102")
        self.assertIn("Title b", needs["payload"]["text"])
        self.assertNotIn("Title a", needs["payload"]["text"])
        self.assertNotIn("Title c", needs["payload"]["text"])
        self.assertIn("Title c", problems["payload"]["text"])
        self.assertNotIn("Title a", problems["payload"]["text"])
        self.assertNotIn("Title b", problems["payload"]["text"])

    def test_duplicate_interaction_is_deterministic_but_new_click_is_fresh(self):
        current = snapshot(item("a", "Ready"))
        first = build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.REFRESH, interaction_id="200")
        duplicate = build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.REFRESH, interaction_id="200")
        later_click = build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.REFRESH, interaction_id="201")
        self.assertEqual(first["outbox_message_id"], duplicate["outbox_message_id"])
        self.assertEqual(first, duplicate)
        self.assertNotEqual(first["outbox_message_id"], later_click["outbox_message_id"])

    def test_snapshot_change_changes_outbox_identity_for_same_interaction(self):
        first = build_l6_outbox_intent(
            snapshot(item("a", "Ready")),
            command=ChannelOSTelegramCommand.REFRESH,
            interaction_id="300",
        )
        changed = build_l6_outbox_intent(
            snapshot(item("a", "Problems", reason="failed")),
            command=ChannelOSTelegramCommand.REFRESH,
            interaction_id="300",
        )
        self.assertNotEqual(first["outbox_message_id"], changed["outbox_message_id"])
        self.assertNotEqual(first["journal_event_ref"], changed["journal_event_ref"])

    def test_junction_intent_is_accepted_by_l6_runtime_as_pending_without_provider_call(self):
        current = snapshot(item("a", "Ready"))
        intent = build_l6_outbox_intent(
            current,
            command=ChannelOSTelegramCommand.REFRESH,
            interaction_id="integration-400",
        )
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            state = root / "state.json"
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "telegram_offset": 0,
                        "sessions": {},
                        "requests": {},
                        "pending_actions": [],
                        "last_event_at": None,
                    }
                ),
                encoding="utf-8",
            )
            request = root / "request.json"
            request.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
            token = root / "token"
            chat = root / "chat"
            token.write_text("bot-token", encoding="utf-8")
            chat.write_text("123", encoding="utf-8")
            env = {
                "TELEGRAM_BOT_TOKEN_FILE": str(token),
                "TELEGRAM_CHAT_ID_FILE": str(chat),
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "scripts.telegram_outbox_runtime.requests.post"
            ) as post:
                queued = enqueue(state, request)
            post.assert_not_called()
            self.assertEqual(queued.status, OutboxStatus.PENDING)
            saved = json.loads(state.read_text(encoding="utf-8"))
            record = saved["telegram_outbox_v1"][intent["outbox_message_id"]]
            self.assertEqual(record["message"]["status"], "PENDING")
            self.assertEqual(record["request"]["payload"]["chat_id"], "123")

    def test_junction_rejects_missing_interaction_or_invalid_page_bound(self):
        current = snapshot(item("a", "Ready"))
        with self.assertRaises(ChannelOSTelegramContractError):
            build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.REFRESH, interaction_id="")
        with self.assertRaises(ChannelOSTelegramContractError):
            build_l6_outbox_intent(current, command=ChannelOSTelegramCommand.REFRESH, interaction_id="x", max_items=0)

    def test_channel_os_junction_has_zero_telegram_io_or_dispatch_ownership(self):
        source = Path("scripts/channel_os_telegram_junction.py").read_text(encoding="utf-8")
        forbidden = (
            "TELEGRAM_BOT_TOKEN",
            "api.telegram.org",
            "getUpdates",
            "requests.post",
            "urllib.request",
            "gh workflow run",
            "subprocess",
        )
        for token in forbidden:
            self.assertNotIn(token, source, token)

    def test_channel_os_domain_has_no_telegram_transport_secret_or_provider_endpoint(self):
        for path in Path("scripts").glob("channel_os*.py"):
            if path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("TELEGRAM_BOT_TOKEN", source, path.name)
            self.assertNotIn("api.telegram.org", source, path.name)
            self.assertNotIn("getUpdates", source, path.name)

    def test_channel_os_still_cannot_upload_or_publish_youtube(self):
        with tempfile.TemporaryDirectory() as root:
            policy = ChannelOSMemory(root).get_policy()
            self.assertFalse(channel_os_youtube_upload_allowed(policy))
            self.assertFalse(channel_os_youtube_publish_allowed(policy))


if __name__ == "__main__":
    unittest.main()
