from __future__ import annotations

import inspect
import unittest
from pathlib import Path

from scripts import telegram_control_active_ui as ui
from scripts import telegram_topic_memory_ui as memory
from scripts import telegram_webhook_replay as replay


def _saved(archive_id: str, kind: str, title: str) -> dict:
    return {
        "schema_version": 1,
        "archive_id": archive_id,
        "status": "available",
        "kind": kind,
        "dedupe_key": ui._suggestion_key(kind, title),
        "saved_at": "2026-08-28T00:00:00+00:00",
        "last_seen_at": "2026-08-28T00:00:00+00:00",
        "candidate": {"title": title, "control_score": 0.8},
    }


def _used(request_id: str, kind: str, topic: str) -> dict:
    return {
        "schema_version": 1,
        "request_id": request_id,
        "kind": kind,
        "topic": topic,
        "dedupe_key": ui._suggestion_key(kind, topic),
        "approval_scope": "short_only" if kind == "short" else "long_only",
        "release_tag": f"{'short' if kind == 'short' else 'video'}-telegram-{request_id}",
        "used_at": "2026-08-28T00:00:00+00:00",
    }


class _CaptureClient:
    def __init__(self):
        self.messages: list[dict] = []

    def send(self, chat_id, text, *, keyboard=None):
        self.messages.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})
        return {"message_id": len(self.messages)}


class TelegramLibrarySplitTests(unittest.TestCase):
    def test_saved_menu_has_independent_long_and_short_counts(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved("l1", "long", "حلقة أولى"),
                _saved("l2", "long", "حلقة ثانية"),
                _saved("s1", "short", "شورت أول"),
            ]
        }
        text, keyboard = memory._saved_kind_menu(state)
        self.assertIn("🎬 طويل — 2", text)
        self.assertIn("⚡ شورت — 1", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "cmd:saved-long")
        self.assertEqual(keyboard[1][0]["callback_data"], "cmd:saved-short")

    def test_saved_long_page_never_mixes_shorts(self):
        state = {
            ui.SAVED_SUGGESTIONS_KEY: [
                _saved("l1", "long", "موضوع طويل فقط"),
                _saved("s1", "short", "موضوع شورت فقط"),
            ]
        }
        text, keyboard = memory._saved_page_by_kind(state, "long", 0)
        self.assertIn("موضوع طويل فقط", keyboard[0][0]["text"])
        self.assertNotIn("موضوع شورت فقط", text)
        self.assertFalse(any("موضوع شورت فقط" in button["text"] for row in keyboard for button in row))
        self.assertEqual(keyboard[0][0]["callback_data"], "cmd:savedpick-l1")
        self.assertEqual(keyboard[-1][0]["callback_data"], "cmd:saved")

    def test_saved_long_topic_can_be_selected_to_scope_choice(self):
        state = {
            "schema_version": 1,
            "sessions": {},
            "requests": {},
            "pending_actions": [],
            ui.SAVED_SUGGESTIONS_KEY: [_saved("l1", "long", "موضوع طويل محفوظ")],
            ui.USED_TOPICS_KEY: [],
        }
        client = _CaptureClient()
        ui._handle_command("savedpick-l1", client, state, None, 7)
        self.assertEqual(len(client.messages), 1)
        message = client.messages[0]
        self.assertIn("موضوع طويل محفوظ", message["text"])
        callback = message["keyboard"][0][0]["callback_data"]
        self.assertRegex(callback, r"^pick:[0-9a-f]{8}:0$")
        session_id = callback.split(":")[1]
        self.assertEqual(state[ui.ACTIVE_RESEARCH_SESSION_KEY], session_id)
        self.assertEqual(state["sessions"][session_id]["kind"], "long")
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)

    def test_saved_short_topic_can_be_selected_to_short_approval(self):
        state = {
            "schema_version": 1,
            "sessions": {},
            "requests": {},
            "pending_actions": [],
            ui.SAVED_SUGGESTIONS_KEY: [_saved("s1", "short", "موضوع شورت محفوظ")],
            ui.USED_TOPICS_KEY: [],
        }
        client = _CaptureClient()
        ui._handle_command("savedpick-s1", client, state, None, 7)
        self.assertEqual(len(client.messages), 1)
        message = client.messages[0]
        self.assertIn("موضوع شورت محفوظ", message["text"])
        callback = message["keyboard"][0][0]["callback_data"]
        self.assertRegex(callback, r"^pickshort:[0-9a-f]{8}:0$")
        session_id = callback.split(":")[1]
        self.assertEqual(state[ui.ACTIVE_RESEARCH_SESSION_KEY], session_id)
        self.assertEqual(state["sessions"][session_id]["kind"], "short")
        self.assertNotIn(ui.PRODUCTION_TARGET_KEY, state)

    def test_used_menu_has_independent_long_and_short_counts(self):
        state = {
            ui.USED_TOPICS_KEY: [
                _used("r1", "long", "حلقة منتجة"),
                _used("r2", "short", "شورت منتج"),
                _used("r3", "short", "شورت منتج ثان"),
            ]
        }
        text, keyboard = memory._used_kind_menu(state)
        self.assertIn("🎬 طويل — 1", text)
        self.assertIn("⚡ شورت — 2", text)
        self.assertEqual(keyboard[0][0]["callback_data"], "cmd:used-long")
        self.assertEqual(keyboard[1][0]["callback_data"], "cmd:used-short")

    def test_used_short_page_never_mixes_long_topics(self):
        state = {
            ui.USED_TOPICS_KEY: [
                _used("r1", "long", "حلقة قديمة"),
                _used("r2", "short", "شورت قديم"),
            ]
        }
        text, keyboard = memory._used_page_by_kind(state, "short", 0)
        self.assertIn("شورت قديم", text)
        self.assertNotIn("حلقة قديمة", text)
        self.assertEqual(keyboard[-1][0]["callback_data"], "cmd:used")

    def test_page_callback_parser_keeps_format_and_page(self):
        self.assertEqual(memory._library_page_request("saved-long", "saved"), ("long", 0))
        self.assertEqual(memory._library_page_request("saved-short-page-3", "saved"), ("short", 3))
        self.assertEqual(memory._library_page_request("used-long-page-2", "used"), ("long", 2))
        self.assertIsNone(memory._library_page_request("saved-long", "used"))

    def test_webhook_replay_installs_exact_library_stack(self):
        source = inspect.getsource(replay.replay_update)
        policy_at = source.index("memory_ui._install_policy()")
        persistent_at = source.index("persistent_ui.install()")
        split_at = source.index("memory_ui._install_library_split()")
        clarity_at = source.index("memory_ui._install_choice_clarity()")
        self.assertLess(policy_at, persistent_at)
        self.assertLess(persistent_at, split_at)
        self.assertLess(split_at, clarity_at)

    def test_every_visible_button_family_has_a_route_and_handler(self):
        root = Path(__file__).resolve().parents[1]
        edge = (root / "cloudflare/telegram-control-worker/observability-worker.js").read_text(encoding="utf-8")
        base = (root / "cloudflare/telegram-control-worker/index.js").read_text(encoding="utf-8")
        panel = (root / "scripts/telegram_control_panel.py").read_text(encoding="utf-8")
        active = (root / "scripts/telegram_control_active_ui.py").read_text(encoding="utf-8")
        persistent = (root / "scripts/telegram_persistent_control_ui.py").read_text(encoding="utf-8")
        memory_source = (root / "scripts/telegram_topic_memory_ui.py").read_text(encoding="utf-8")
        replay_source = (root / "scripts/telegram_webhook_replay.py").read_text(encoding="utf-8")

        # Edge-local navigation/read surfaces with literal callback names.
        for callback in (
            "cmd:menu",
            "cmd:search_menu",
            "cmd:library_menu",
            "cmd:status",
            "cmd:refresh_all",
            "cmd:saved",
            "cmd:used",
        ):
            self.assertIn(callback, edge, callback)
        # The four Long/Short library buttons are generated from one canonical bucket template.
        self.assertIn('callback_data: `cmd:${bucket}-long`', edge)
        self.assertIn('callback_data: `cmd:${bucket}-short`', edge)
        self.assertIn('/^cmd:(saved|used)-(long|short)', edge)
        self.assertIn("pageSpec(data)", edge)
        self.assertIn("return baseWorker.fetch(request, env, ctx)", edge)

        # Read-only leaves handled by the base Worker.
        for callback in (
            "cmd:last_delivery",
            "cmd:stats_menu",
            "cmd:stats_last_long",
            "cmd:stats_last_short",
            "cmd:stats_today",
            "cmd:stats_week",
            "cmd:stats_overview",
        ):
            self.assertIn(callback, base, callback)

        # Stateful callbacks are forwarded to GitHub and consumed by Python.
        self.assertIn("dispatchToGitHub(env, update)", base)
        self.assertIn('callback_data: `cmd:savedpick-${String(item.archive_id || "")}`', edge)
        self.assertIn('kind.startswith("savedpick-")', active)
        self.assertIn('parts[0] == "detail"', panel)
        self.assertIn('parts[0] == "pickshort"', panel)
        self.assertIn('parts[0] == "pick"', panel)
        self.assertIn('parts[0] == "scope"', panel)
        self.assertIn('parts[0] == "refresh"', panel)
        self.assertIn('"callback_data": f"{pick}:{session_id}:{index}"', persistent)
        self.assertIn('"callback_data": f"detail:{session_id}:{index}"', persistent)
        self.assertIn('"callback_data": f"refresh:{kind}"', persistent)
        self.assertIn('"callback_data": f"scope:{parts[1]}:{index}:bundle"', panel)
        self.assertIn('"callback_data": f"scope:{parts[1]}:{index}:long"', panel)

        # Search callbacks reach the Python research queue; saved split is installed in replay.
        self.assertIn('callback_data: "cmd:topic"', base)
        self.assertIn('callback_data: "cmd:short"', base)
        self.assertIn('if kind in {"topic", "short"}', active)
        self.assertIn("memory_ui._install_library_split()", replay_source)
        self.assertIn('base = f"{bucket}-{kind}"', memory_source)

        # Production remains an explicit text confirmation, never a read-button side effect.
        self.assertIn('PRODUCTION_CONFIRMATION_TEXT = "تأكيد الإنتاج"', persistent)
        self.assertIn('CONFIRM_TEXT = "تأكيد الإنتاج"', base)
        self.assertIn('if (value === CONFIRM_TEXT) return "stateful"', base)
        self.assertIn('route === "stateful"', base)

        # Operations details/compact buttons are self-bound and handled locally.
        self.assertIn("cmd:ops(details|compact)-", base)
        self.assertIn("cmd:ops${next}-", base)
        self.assertIn("handleOperationsToggle", base)


if __name__ == "__main__":
    unittest.main()
