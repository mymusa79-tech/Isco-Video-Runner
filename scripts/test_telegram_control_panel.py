from __future__ import annotations

import copy
import unittest

from scripts import telegram_control_panel as panel


class TelegramEditorialControlPanelTests(unittest.TestCase):
    def test_arabic_command_routes_are_small_and_explicit(self):
        self.assertEqual(panel._command_kind("أريد موضوع"), "topic")
        self.assertEqual(panel._command_kind("اريد شورت"), "short")
        self.assertEqual(panel._command_kind("آخر فيديو"), "last_long")
        self.assertEqual(panel._command_kind("اخر شورت"), "last_short")
        self.assertEqual(panel._command_kind("الحالة"), "status")
        self.assertIsNone(panel._command_kind("انشر الفيديو الآن"))

    def test_main_panel_contains_no_publish_action(self):
        keyboard = panel._main_keyboard()
        flattened = [button for row in keyboard for button in row]
        labels = " ".join(button.get("text", "") for button in flattened)
        callbacks = " ".join(button.get("callback_data", "") for button in flattened)
        self.assertNotIn("انشر", labels)
        self.assertNotIn("publish", callbacks.casefold())
        self.assertIn("اقتراح حلقة", labels)
        self.assertIn("آخر فيديو", labels)

    def test_candidate_callback_data_respects_telegram_limit(self):
        keyboard = panel._candidate_keyboard("deadbeef", "long")
        for button in [button for row in keyboard for button in row]:
            callback = button.get("callback_data")
            if callback:
                self.assertLessEqual(len(callback.encode("utf-8")), 64)

    def test_research_queue_deduplicates_same_kind(self):
        state = panel._new_state()
        self.assertTrue(panel._queue_research(state, "long", 123))
        self.assertFalse(panel._queue_research(state, "long", 123))
        self.assertTrue(panel._queue_research(state, "short", 123))
        self.assertEqual(len(state["pending_actions"]), 2)

    def test_short_candidate_adds_admission_evidence(self):
        raw = {
            "title": "اختبار",
            "pillar": "rise",
            "format_hint": "film",
            "hook_potential": 0.9,
            "retention_potential": 0.8,
            "emotional_pull": 0.8,
            "audience_fit": 0.9,
            "title_thumbnail_potential": 0.7,
            "production_feasibility": 0.9,
            "evidence_quality": 0.8,
        }
        item = panel._build_candidate_payload(raw, "short")
        admission = item["short_admission"]
        self.assertEqual(item["format_hint"], "moment")
        self.assertGreaterEqual(admission["short_fit_score"], 7.0)
        self.assertGreaterEqual(admission["immediate_action_score"], 7.0)
        self.assertTrue(admission["single_action_contract"])

    def test_approved_long_bundle_never_authorizes_dispatch(self):
        state = panel._new_state()
        session = {
            "session_id": "abcd1234",
            "kind": "long",
            "candidates": [{
                "title": "موضوع موثق",
                "format_hint": "film",
                "evidence": ["signal one"],
                "control_score": 0.9,
            }],
        }
        request = panel._approve(state, session, 0, "bundle")
        self.assertEqual(request["approval_scope"], "long_plus_sibling_shorts")
        self.assertEqual(request["sibling_shorts"]["minimum"], 2)
        self.assertEqual(request["sibling_shorts"]["maximum"], 3)
        self.assertFalse(request["production_dispatch_authorized"])
        self.assertEqual(request["status"], "approved_waiting_production_activation")
        copy_without_hash = copy.deepcopy(request)
        digest = copy_without_hash.pop("request_sha256")
        self.assertEqual(digest, panel._canonical_hash(copy_without_hash))

    def test_approved_native_short_never_authorizes_dispatch(self):
        state = panel._new_state()
        session = {
            "session_id": "abcd1234",
            "kind": "short",
            "candidates": [{
                "title": "فكرة شورت",
                "format_hint": "moment",
                "evidence": [],
                "short_admission": {"short_fit_score": 9.0},
            }],
        }
        request = panel._approve(state, session, 0, "short")
        self.assertEqual(request["approval_scope"], "short_only")
        self.assertEqual(request["format"], "moment")
        self.assertFalse(request["production_dispatch_authorized"])

    def test_authorization_requires_user_and_optional_chat(self):
        update = {"message": {"from": {"id": 77}, "chat": {"id": 88}, "text": "الحالة"}}
        self.assertEqual(panel._authorized_user(update, 77, "88")[0], True)
        self.assertEqual(panel._authorized_user(update, 78, "88")[0], False)
        self.assertEqual(panel._authorized_user(update, 77, "99")[0], False)

    def test_release_message_is_compact(self):
        release = {
            "tag_name": "video-42",
            "published_at": "2026-08-22T12:00:00Z",
            "assets": [{"name": "final.mp4"}],
        }
        text = panel._format_release(release, "long")
        self.assertIn("video-42", text)
        self.assertIn("حزمة جاهزة", text)
        self.assertLess(len(text), 300)


if __name__ == "__main__":
    unittest.main()
