from __future__ import annotations

import unittest
from unittest import mock

from scripts import telegram_control_active_ui as ui
from scripts import telegram_topic_memory_ui as memory


def _candidate(title: str) -> dict:
    return {
        "title": title,
        "control_score": 0.8,
        "opportunity_score": 0.8,
        "approved_research_pack": [
            {"source_title": "A", "source_url": "https://example.com/a", "claim_scope": "a"},
            {"source_title": "B", "source_url": "https://example.com/b", "claim_scope": "b"},
        ],
    }


def _request(request_id: str, *, kind: str, topic: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "kind": kind,
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "approved_topic": topic,
        "approved_at": "2026-08-23T18:00:00+00:00",
        "request_sha256": "x" * 64,
    }


class TelegramTopicMemoryPolicyTests(unittest.TestCase):
    def setUp(self):
        memory._install_policy()

    def test_completed_short_blocks_same_topic_from_long_research(self):
        state = {ui.USED_TOPICS_KEY: []}
        ui._mark_request_used(
            state,
            _request("req-short", kind="short", topic="لماذا نخاف من رأي الآخرين؟"),
            release_tag="short-telegram-req-short",
            used_at="2026-08-23T18:00:00+00:00",
        )
        kept, blocked = ui._filter_used_candidates(
            state,
            "long",
            [_candidate("الخوف من رأي الآخرين"), _candidate("موضوع جديد تمامًا")],
        )
        self.assertEqual(blocked, 1)
        self.assertEqual([item["title"] for item in kept], ["موضوع جديد تمامًا"])

    def test_completed_long_blocks_same_topic_from_short_research(self):
        state = {ui.USED_TOPICS_KEY: []}
        ui._mark_request_used(
            state,
            _request("req-long", kind="long", topic="استنزاف طاقتك في إرضاء الآخرين"),
            release_tag="video-telegram-req-long",
            used_at="2026-08-23T18:00:00+00:00",
        )
        kept, blocked = ui._filter_used_candidates(
            state,
            "short",
            [_candidate("لماذا نستنزف طاقتنا في محاولة إرضاء الآخرين؟"), _candidate("فكرة شورت جديدة")],
        )
        self.assertEqual(blocked, 1)
        self.assertEqual([item["title"] for item in kept], ["فكرة شورت جديدة"])

    def test_approval_keeps_saved_idea_until_successful_production(self):
        saved = {
            "schema_version": 1,
            "archive_id": "saved-1",
            "status": "available",
            "kind": "long",
            "dedupe_key": ui._suggestion_key("long", "موضوع محفوظ"),
            "saved_at": "2026-08-23T18:00:00+00:00",
            "last_seen_at": "2026-08-23T18:00:00+00:00",
            "candidate": _candidate("موضوع محفوظ"),
        }
        session = {
            "session_id": "session-current",
            "kind": "long",
            "candidates": [saved["candidate"]],
            "saved_suggestion_ids": ["saved-1"],
        }
        request = _request("req-selected", kind="long", topic="موضوع محفوظ")
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [saved],
            "requests": {},
            "production_queue": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
        }
        with mock.patch.object(ui.simple, "_approve", return_value=request):
            approved = ui._approve_current(state, session, 0, "long")
        self.assertIs(approved, request)
        self.assertEqual(len(ui._available_saved(state)), 1)
        self.assertEqual(ui._used_topics(state), [])
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["request_id"], "req-selected")

    def test_successful_production_then_removes_saved_idea_and_blocks_all_formats(self):
        topic = "موضوع محفوظ"
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                {
                    "schema_version": 1,
                    "archive_id": "saved-1",
                    "status": "available",
                    "kind": "long",
                    "dedupe_key": ui._suggestion_key("long", topic),
                    "saved_at": "2026-08-23T18:00:00+00:00",
                    "last_seen_at": "2026-08-23T18:00:00+00:00",
                    "candidate": _candidate(topic),
                }
            ],
            ui.USED_TOPICS_KEY: [],
        }
        ui._mark_request_used(
            state,
            _request("req-done", kind="long", topic=topic),
            release_tag="video-telegram-req-done",
            used_at="2026-08-23T18:05:00+00:00",
        )
        self.assertEqual(ui._available_saved(state), [])
        self.assertTrue(ui._is_used_topic(state, "long", topic))
        self.assertTrue(ui._is_used_topic(state, "short", topic))

    def test_policy_does_not_overblock_different_topic(self):
        state = {ui.USED_TOPICS_KEY: []}
        ui._mark_request_used(
            state,
            _request("req-old", kind="long", topic="الخوف من الفشل"),
            release_tag="video-telegram-req-old",
            used_at="2026-08-23T18:00:00+00:00",
        )
        self.assertFalse(ui._is_used_topic(state, "short", "الخوف من النجاح"))


if __name__ == "__main__":
    unittest.main()
