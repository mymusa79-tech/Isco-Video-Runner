from __future__ import annotations

import os
import unittest
from unittest import mock

from scripts import telegram_control_active_ui as ui
from scripts import telegram_production_queue as queue


class _Client:
    def __init__(self):
        self.messages = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append((chat_id, text, keyboard))


def _request(request_id: str = "req-ui-1", *, approved_at: str = "2026-08-23T07:00:00+00:00", kind: str = "long", topic: str | None = None) -> dict:
    item = {
        "schema_version": 1,
        "request_id": request_id,
        "source": "telegram_editorial_control_panel",
        "kind": kind,
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "approved_topic": topic or f"موضوع معتمد {request_id}",
        "approved_at": approved_at,
        "approved_by_user": True,
        "production_dispatch_authorized": False,
        "status": "approved_waiting_production_activation",
    }
    item["request_sha256"] = queue._request_hash(item)
    return item


def _target(request: dict, session_id: str = "session-current") -> dict:
    return {
        "request_id": request["request_id"],
        "request_sha256": request["request_sha256"],
        "session_id": session_id,
        "selected_at": request["approved_at"],
    }


def _candidate(title: str, *, score: float = 0.8) -> dict:
    return {
        "title": title,
        "control_score": score,
        "opportunity_score": score,
        "audience_fit": score,
        "hook_potential": score,
        "retention_potential": score,
        "title_thumbnail_potential": score,
        "evergreen_score": score,
        "trend_score": score,
        "production_feasibility": score,
        "evidence_quality": score,
        "evidence": ["دليل محفوظ"],
        "why": ["ملاءمة قوية لجمهور القناة"],
        "approved_research_pack": [
            {"source_title": "Source A", "source_url": "https://example.com/a", "claim_scope": "scope a"},
            {"source_title": "Source B", "source_url": "https://example.com/b", "claim_scope": "scope b"},
        ],
    }


def _saved_item(
    title: str,
    *,
    kind: str = "long",
    score: float = 0.8,
    saved_at: str = "2026-08-01T00:00:00+00:00",
    missed_reviews: int = 0,
    archive_id: str = "saved-1",
) -> dict:
    return {
        "schema_version": 1,
        "archive_id": archive_id,
        "status": "available",
        "kind": kind,
        "dedupe_key": ui._suggestion_key(kind, title),
        "saved_at": saved_at,
        "last_seen_at": saved_at,
        "review_count": 0,
        "missed_reviews": missed_reviews,
        "candidate": _candidate(title, score=score),
    }


class TelegramActiveUiTests(unittest.TestCase):
    def test_enabled_surface_has_saved_used_and_one_explicit_start_button(self):
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            buttons = [button for row in ui._main_keyboard() for button in row]
            starts = [button for button in buttons if button.get("callback_data") == "cmd:produce_latest"]
            self.assertEqual(len(starts), 1)
            self.assertIn("🚀", starts[0]["text"])
            self.assertEqual(len([button for button in buttons if button.get("callback_data") == "cmd:saved"]), 1)
            self.assertEqual(len([button for button in buttons if button.get("callback_data") == "cmd:used"]), 1)
            menu = ui._menu_text()
            self.assertIn("إنتاج Telegram مفعّل", menu)
            self.assertIn("ضغطة مستقلة", menu)
            self.assertIn("لا نشر إلى YouTube", menu)
            self.assertIn("المستعملة", menu)

    def test_approval_never_auto_starts_production_or_marks_topic_used(self):
        state = {"requests": {}, "production_queue": [], ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current"}
        request = _request()
        session = {"session_id": "session-current", "kind": "long", "candidates": []}
        with mock.patch.object(ui.simple, "_approve", return_value=request):
            ui._approve_current(state, session, 0, "long")
        self.assertEqual(ui._used_topics(state), [])
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            text = ui._approval_text(request)
        self.assertIn("لم يبدأ الإنتاج بعد", text)
        self.assertIn("ابدأ الإنتاج المعتمد", text)

    def test_old_saved_approval_cannot_start_without_current_research_target(self):
        old = _request("req-old")
        state = {"requests": {old["request_id"]: old}, "production_queue": [], "last_event_at": None}
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(state["production_queue"], [])
        self.assertIn("جلسة الاختيار الحالية", client.messages[-1][1])

    def test_start_button_queues_exact_current_selection_not_latest_saved_request(self):
        selected = _request("req-selected", approved_at="2026-08-23T07:00:00+00:00")
        newer_saved = _request("req-newer-saved", approved_at="2026-08-23T08:00:00+00:00")
        state = {
            "requests": {selected["request_id"]: selected, newer_saved["request_id"]: newer_saved},
            "production_queue": [],
            "last_event_at": None,
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            ui.PRODUCTION_TARGET_KEY: _target(selected),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "true"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
        self.assertEqual(len(state["production_queue"]), 1)
        action = state["production_queue"][0]
        self.assertEqual(action["request_id"], "req-selected")
        self.assertEqual(action["request_sha256"], selected["request_sha256"])
        self.assertFalse(selected["production_dispatch_authorized"])
        self.assertIn("تم تأكيد بدء الإنتاج", client.messages[-1][1])

    def test_starting_new_research_invalidates_previous_production_target(self):
        selected = _request("req-selected")
        state = {
            "requests": {selected["request_id"]: selected},
            "production_queue": [],
            "pending_actions": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-old",
            ui.PRODUCTION_TARGET_KEY: _target(selected, "session-old"),
        }
        client = _Client()
        with mock.patch.object(ui.simple, "_handle_command") as delegated:
            ui._handle_command("topic", client, state, None, 77)
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)
        self.assertNotIn(ui.ACTIVE_RESEARCH_SESSION_KEY, state)
        delegated.assert_called_once_with("topic", client, state, None, 77)

    def test_stale_research_session_cannot_become_production_target(self):
        state = {ui.ACTIVE_RESEARCH_SESSION_KEY: "session-new"}
        stale = {"session_id": "session-old", "kind": "long", "candidates": []}
        with mock.patch.object(ui.simple, "_approve") as approve:
            with self.assertRaisesRegex(RuntimeError, "current research session"):
                ui._approve_current(state, stale, 0, "long")
        approve.assert_not_called()
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)

    def test_disabled_surface_cannot_queue_production(self):
        selected = _request()
        state = {
            "requests": {selected["request_id"]: selected},
            "production_queue": [],
            ui.ACTIVE_RESEARCH_SESSION_KEY: "session-current",
            ui.PRODUCTION_TARGET_KEY: _target(selected),
        }
        client = _Client()
        with mock.patch.dict(os.environ, {"CONTROL_PLANE_PRODUCTION_ENABLED": "false"}, clear=False):
            ui._handle_command("produce_latest", client, state, None, 77)
            buttons = [button for row in ui._main_keyboard() for button in row]
        self.assertEqual(state["production_queue"], [])
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertIn("مقفول", client.messages[-1][1])

    def test_research_candidates_are_saved_as_non_executable_ideas_and_deduplicated(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        first = {"session_id": "s1", "kind": "long", "candidates": [_candidate("أ"), _candidate("ب"), _candidate("ج")]}
        ids = ui._archive_session_candidates(state, first)
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(ui._available_saved(state)), 3)
        for item in ui._available_saved(state):
            self.assertNotIn("request_id", item)
            self.assertNotIn("production_dispatch_authorized", item)
        second = {"session_id": "s2", "kind": "long", "candidates": [_candidate("  أ  ", score=0.9)]}
        second_ids = ui._archive_session_candidates(state, second)
        self.assertEqual(second_ids[0], ids[0])
        self.assertEqual(len(ui._available_saved(state)), 3)

    def test_saved_archive_is_bounded_and_current_candidates_are_protected(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        for index in range(ui.MAX_SAVED_SUGGESTIONS):
            state.setdefault(ui.SAVED_SUGGESTIONS_KEY, []).append(
                _saved_item(f"قديم {index}", score=0.95, archive_id=f"old-{index}")
            )
        session = {
            "session_id": "fresh",
            "kind": "short",
            "candidates": [_candidate("جديد 1", score=0.2), _candidate("جديد 2", score=0.2), _candidate("جديد 3", score=0.2)],
        }
        ids = ui._archive_session_candidates(state, session)
        live = {item["archive_id"] for item in ui._available_saved(state)}
        self.assertEqual(len(live), ui.MAX_SAVED_SUGGESTIONS)
        self.assertTrue(set(ids).issubset(live))

    def test_saved_review_is_scoped_by_kind(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved_item("حلقة قديمة", kind="long", archive_id="long"),
                _saved_item("شورت قديم", kind="short", archive_id="short"),
            ]
        }
        ui._review_saved_suggestions(state, "long", [_candidate("حلقة أخرى")], now_text="2026-08-23T17:00:00+00:00")
        by_id = {item["archive_id"]: item for item in ui._available_saved(state)}
        self.assertEqual(by_id["long"]["missed_reviews"], 1)
        self.assertEqual(by_id["short"]["missed_reviews"], 0)
        self.assertNotIn("last_reviewed_at", by_id["short"])

    def test_two_missed_searches_never_delete_even_very_weak_old_idea(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved_item("لا تحذفني سريعًا", score=0.1, saved_at="2025-01-01T00:00:00+00:00", missed_reviews=1)
            ]
        }
        report = ui._review_saved_suggestions(state, "long", [_candidate("غيرها")], now_text="2026-08-23T17:00:00+00:00")
        self.assertEqual(len(ui._available_saved(state)), 1)
        self.assertEqual(ui._available_saved(state)[0]["missed_reviews"], 2)
        self.assertEqual(report["pruned"], [])

    def test_weak_old_idea_prunes_on_third_missed_same_kind_review(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved_item("ضعيفة قديمة", score=0.4, saved_at="2026-07-01T00:00:00+00:00", missed_reviews=2)
            ]
        }
        report = ui._review_saved_suggestions(state, "long", [_candidate("غيرها")], now_text="2026-08-23T17:00:00+00:00")
        self.assertEqual(ui._available_saved(state), [])
        self.assertEqual(report["pruned"][0]["reason"], "weak_and_repeatedly_absent")

    def test_reappearing_saved_idea_resets_missed_reviews_and_refreshes_score(self):
        item = _saved_item("أين تذهب طاقتك؟", score=0.4, saved_at="2026-05-01T00:00:00+00:00", missed_reviews=5)
        state = {ui.SAVED_SUGGESTIONS_KEY: [item]}
        report = ui._review_saved_suggestions(
            state,
            "long",
            [_candidate("أين تذهب طاقتك؟", score=0.91)],
            now_text="2026-08-23T17:00:00+00:00",
        )
        kept = ui._available_saved(state)[0]
        self.assertEqual(kept["missed_reviews"], 0)
        self.assertEqual(kept["candidate"]["control_score"], 0.91)
        self.assertEqual(report["refreshed"], 1)

    def test_arabic_title_normalization_and_small_wording_change_match_same_topic(self):
        self.assertEqual(ui._suggestion_key("long", "أَيْنَ تذهبُ طاقتك؟"), ui._suggestion_key("long", "اين تذهب طاقتك"))
        self.assertTrue(
            ui._same_topic_title(
                "لماذا نستنزف طاقتنا في محاولة إرضاء الآخرين؟",
                "استنزاف طاقتنا في محاولة إرضاء الآخرين",
            )
        )
        self.assertFalse(ui._same_topic_title("الخوف من الفشل", "الخوف من النجاح"))

    def test_successful_request_moves_topic_to_used_and_removes_saved_copy(self):
        topic = "لماذا نستنزف طاقتنا في إرضاء الآخرين؟"
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [_saved_item(topic, kind="long", archive_id="same")],
            ui.USED_TOPICS_KEY: [],
        }
        request = _request("req-produced", kind="long", topic=topic)
        record = ui._mark_request_used(
            state,
            request,
            release_tag="telegram-req-produced",
            used_at="2026-08-23T17:00:00+00:00",
        )
        self.assertEqual(record["topic"], topic)
        self.assertEqual(len(ui._used_topics(state)), 1)
        self.assertEqual(ui._available_saved(state), [])

    def test_used_topic_is_excluded_before_new_top_three_but_next_candidates_remain(self):
        state = {ui.USED_TOPICS_KEY: []}
        used = _request("req-used", kind="long", topic="لماذا نستنزف طاقتنا في محاولة إرضاء الآخرين؟")
        ui._mark_request_used(state, used, release_tag="rel-used", used_at="2026-08-20T00:00:00+00:00")
        candidates = [
            _candidate("استنزاف طاقتنا في محاولة إرضاء الآخرين"),
            _candidate("موضوع جديد 1"),
            _candidate("موضوع جديد 2"),
            _candidate("موضوع جديد 3"),
        ]
        kept, blocked = ui._filter_used_candidates(state, "long", candidates)
        self.assertEqual(blocked, 1)
        self.assertEqual([item["title"] for item in kept[:3]], ["موضوع جديد 1", "موضوع جديد 2", "موضوع جديد 3"])

    def test_used_topic_block_is_format_scoped(self):
        state = {ui.USED_TOPICS_KEY: []}
        used_short = _request("req-short", kind="short", topic="الخوف من رأي الآخرين")
        ui._mark_request_used(state, used_short, release_tag="rel-short", used_at="2026-08-20T00:00:00+00:00")
        long_kept, long_blocked = ui._filter_used_candidates(state, "long", [_candidate("الخوف من رأي الآخرين")])
        short_kept, short_blocked = ui._filter_used_candidates(state, "short", [_candidate("الخوف من رأي الآخرين")])
        self.assertEqual(long_blocked, 0)
        self.assertEqual(len(long_kept), 1)
        self.assertEqual(short_blocked, 1)
        self.assertEqual(short_kept, [])

    def test_saved_topic_that_became_used_cannot_be_activated(self):
        topic = "موضوع تم إنتاجه"
        state = {
            "sessions": {},
            "pending_actions": [],
            ui.SAVED_SUGGESTIONS_KEY: [_saved_item(topic, archive_id="stale")],
            ui.USED_TOPICS_KEY: [],
        }
        ui._mark_request_used(
            state,
            _request("req-done", topic=topic),
            release_tag="rel-done",
            used_at="2026-08-23T17:00:00+00:00",
        )
        self.assertEqual(ui._available_saved(state), [])
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            ui._activate_saved_suggestion(state, "stale")

    def test_used_page_is_read_only(self):
        state = {ui.USED_TOPICS_KEY: []}
        for index in range(7):
            ui._mark_request_used(
                state,
                _request(f"req-{index}", topic=f"موضوع منتج {index}"),
                release_tag=f"rel-{index}",
                used_at=f"2026-08-{10 + index:02d}T00:00:00+00:00",
            )
        text, keyboard = ui._used_page(state, 0)
        buttons = [button for row in keyboard for button in row]
        self.assertIn("صفحة 1/2", text)
        self.assertFalse(any(str(button.get("callback_data", "")).startswith("cmd:savedpick-") for button in buttons))
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertTrue(any(button.get("callback_data") == "cmd:usedpage-1" for button in buttons))

    def test_opening_saved_idea_creates_fresh_session_without_approval_or_queue(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "production_queue": []}
        source = {"session_id": "s1", "kind": "long", "candidates": [_candidate("موضوع محفوظ")]}
        archive_id = ui._archive_session_candidates(state, source)[0]
        session = ui._activate_saved_suggestion(state, archive_id)
        self.assertEqual(session["source"], "saved_suggestion")
        self.assertEqual(state[ui.ACTIVE_RESEARCH_SESSION_KEY], session["session_id"])
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)
        self.assertEqual(state["requests"], {})
        self.assertEqual(state["production_queue"], [])

    def test_approving_saved_idea_removes_only_it_and_binds_exact_target(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": [], "production_queue": []}
        source = {"session_id": "s1", "kind": "long", "candidates": [_candidate("اخترني"), _candidate("ابقني")]}
        ids = ui._archive_session_candidates(state, source)
        session = ui._activate_saved_suggestion(state, ids[0])
        request = _request("req-saved")
        state["requests"][request["request_id"]] = request
        with mock.patch.object(ui.simple, "_approve", return_value=request):
            ui._approve_current(state, session, 0, "long")
        remaining = [item["candidate"]["title"] for item in ui._available_saved(state)]
        self.assertEqual(remaining, ["ابقني"])
        self.assertEqual(state[ui.PRODUCTION_TARGET_KEY]["request_id"], "req-saved")
        self.assertEqual(ui._used_topics(state), [])

    def test_saved_pick_waits_for_running_research(self):
        state = {
            "sessions": {},
            "requests": {},
            "production_queue": [],
            "pending_actions": [{"kind": "long", "status": "pending"}],
        }
        source = {"session_id": "s1", "kind": "long", "candidates": [_candidate("موضوع محفوظ")]}
        archive_id = ui._archive_session_candidates(state, source)[0]
        with self.assertRaisesRegex(RuntimeError, "research to finish"):
            ui._activate_saved_suggestion(state, archive_id)
        self.assertNotIn(ui.ACTIVE_RESEARCH_SESSION_KEY, state)

    def test_saved_page_is_paginated_and_has_no_direct_start_button(self):
        state = {"sessions": {}, "requests": {}, "pending_actions": []}
        session = {"session_id": "s1", "kind": "short", "candidates": [_candidate(f"فكرة {index}") for index in range(7)]}
        ui._archive_session_candidates(state, session)
        text, keyboard = ui._saved_page(state, 0)
        buttons = [button for row in keyboard for button in row]
        self.assertIn("صفحة 1/2", text)
        self.assertEqual(sum(str(button.get("callback_data", "")).startswith("cmd:savedpick-") for button in buttons), 5)
        self.assertFalse(any(button.get("callback_data") == "cmd:produce_latest" for button in buttons))
        self.assertTrue(any(button.get("callback_data") == "cmd:savedpage-1" for button in buttons))

    def test_saved_and_used_command_aliases(self):
        self.assertEqual(ui._command_kind("الاقتراحات المحفوظة"), "saved")
        self.assertEqual(ui._command_kind("المحفوظة"), "saved")
        self.assertEqual(ui._command_kind("المواضيع المستعملة"), "used")
        self.assertEqual(ui._command_kind("المنتجة"), "used")


if __name__ == "__main__":
    unittest.main()
