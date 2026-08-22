from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import telegram_control_simple_ui as ui


class SimpleTelegramUiTests(unittest.TestCase):
    def test_main_surface_has_exactly_three_entries(self):
        keyboard = ui._main_keyboard()
        labels = [button[0]["text"] for button in keyboard]
        self.assertEqual(labels, ["✨ اقترح", "🎁 آخر إنتاج", "🧭 الحالة"])

    def test_main_surface_has_no_publish_upload_or_schedule_action(self):
        payload = str(ui._main_keyboard()) + ui._menu_text()
        lowered = payload.casefold()
        self.assertNotIn("publish", lowered)
        self.assertNotIn("upload", lowered)
        self.assertNotIn("schedule", lowered)
        self.assertIn("لا نشر", payload)
        self.assertIn("يدوي", payload)

    def test_delivery_view_combines_long_and_short(self):
        long_release = {"tag_name": "video-10", "published_at": "2026-08-22T10:00:00Z", "html_url": "https://example.com/long"}
        short_release = {"tag_name": "short-9", "published_at": "2026-08-22T11:00:00Z", "html_url": "https://example.com/short"}
        text = ui._last_delivery_text(long_release, short_release)
        self.assertIn("video-10", text)
        self.assertIn("short-9", text)
        self.assertIn("نشر يدوي", text)
        buttons = [button for row in ui._delivery_keyboard(long_release, short_release) for button in row]
        labels = " ".join(button["text"] for button in buttons)
        self.assertIn("حزمة الفيديو", labels)
        self.assertIn("آخر شورت", labels)
        self.assertIn("A/B/C", labels)

    def test_suggest_surface_asks_only_content_type(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
            def send(self, chat_id, text, *, keyboard=None):
                self.calls.append((chat_id, text, keyboard))

        fake = FakeClient()
        ui._handle_command("suggest", fake, {}, None, 123)
        _, text, keyboard = fake.calls[0]
        self.assertIn("ماذا تريد", text)
        labels = [row[0]["text"] for row in keyboard]
        self.assertEqual(labels[:2], ["🎬 حلقة", "📱 شورت"])

    def test_natural_command_maps_to_simple_suggest(self):
        self.assertEqual(ui._command_kind("اقترح"), "suggest")
        self.assertEqual(ui._command_kind("آخر إنتاج"), "last_delivery")

    def test_crossref_sources_are_structurally_approved_brief_compatible(self):
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1000/example-one",
                        "title": ["Self regulation and everyday behavior"],
                        "container-title": ["Example Journal"],
                    },
                    {
                        "DOI": "10.1000/example-two",
                        "title": ["Motivation and goal pursuit"],
                        "container-title": ["Another Journal"],
                    },
                ]
            }
        }

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("scripts.telegram_control_simple_ui.urllib.request.urlopen", return_value=FakeResponse()):
            sources = ui._crossref_sources("self regulation motivation", "موضوع", limit=2)
        self.assertEqual(len(sources), 2)
        for source in sources:
            self.assertTrue(source["source_title"])
            self.assertTrue(source["source_url"].startswith("https://doi.org/"))
            self.assertTrue(source["claim_scope"])
            self.assertEqual(source["metadata_registry"], "Crossref REST API")

    def test_long_approval_carries_research_pack_and_rehashes(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "last_event_at": None}
        source = {
            "source_title": "Study A",
            "source_url": "https://doi.org/10.1000/a",
            "claim_scope": "background only",
        }
        source2 = {
            "source_title": "Study B",
            "source_url": "https://doi.org/10.1000/b",
            "claim_scope": "background only",
        }
        session = {
            "session_id": "abc",
            "kind": "long",
            "candidates": [
                {
                    "title": "موضوع الحلقة",
                    "format_hint": "film",
                    "evidence": ["signal"],
                    "approved_research_pack": [source, source2],
                }
            ],
        }
        request = ui._approve(state, session, 0, "long")
        self.assertEqual(request["approved_research_pack"], [source, source2])
        self.assertNotIn("research_pack", request)
        stored_hash = request["request_sha256"]
        subject = dict(request)
        subject.pop("request_sha256")
        self.assertEqual(stored_hash, ui.panel._canonical_hash(subject))
        self.assertFalse(request["production_dispatch_authorized"])

    def test_long_approval_fails_without_two_scholarly_sources(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "last_event_at": None}
        session = {
            "session_id": "abc",
            "kind": "long",
            "candidates": [{"title": "موضوع", "format_hint": "film", "approved_research_pack": []}],
        }
        with self.assertRaisesRegex(RuntimeError, "scholarly research pack"):
            ui._approve(state, session, 0, "long")


if __name__ == "__main__":
    unittest.main()
