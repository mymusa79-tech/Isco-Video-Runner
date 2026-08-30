from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import telegram_control_panel as panel
from scripts import telegram_webhook_replay as replay


def _state(path: Path) -> None:
    panel.save_state(
        path,
        {
            "schema_version": 1,
            "telegram_offset": 0,
            "sessions": {},
            "requests": {},
            "pending_actions": [],
            "production_queue": [],
            "saved_suggestions": [
                {
                    "schema_version": 1,
                    "archive_id": "saved-1",
                    "status": "available",
                    "kind": "short",
                    "saved_at": "2026-08-30T08:00:00+00:00",
                    "last_seen_at": "2026-08-30T08:00:00+00:00",
                    "candidate": {
                        "title": "كيف تنهض عندما تفقد الدافع تمامًا؟",
                        "control_score": 0.91,
                        "why": ["خطاف واضح", "ملائم للقناة"],
                        "evidence": [],
                    },
                }
            ],
            "used_topics": [],
            "last_event_at": None,
        },
    )


def _saved_pick_update() -> dict:
    return {
        "update_id": 501,
        "callback_query": {
            "id": "cb-saved-1",
            "from": {"id": 88},
            "message": {"message_id": 42, "chat": {"id": 77}},
            "data": "cmd:savedpick-saved-1",
        },
    }


def _secret(name: str, *, required: bool = False) -> str:
    values = {
        "TELEGRAM_BOT_TOKEN_FILE": "token",
        "TELEGRAM_ALLOWED_USER_ID_FILE": "88",
        "TELEGRAM_CHAT_ID_FILE": "77",
    }
    value = values.get(name, "")
    if required and not value:
        raise RuntimeError(f"missing test secret: {name}")
    return value


class TelegramSavedReplayE2ETests(unittest.TestCase):
    def test_saved_pick_replay_reaches_detail_without_production_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            _state(path)
            sent: list[dict] = []

            def fake_call(self, method: str, payload=None):
                if method == "sendMessage":
                    sent.append(dict(payload or {}))
                    return {"message_id": 900}
                if method == "sendRichMessage":
                    raise RuntimeError("force tested text fallback")
                if method == "answerCallbackQuery":
                    return True
                raise AssertionError(f"unexpected Telegram method: {method}")

            with mock.patch.object(panel, "_read_secret_file", side_effect=_secret), \
                 mock.patch.object(panel.TelegramClient, "call", new=fake_call):
                self.assertTrue(replay.replay_update(path, _saved_pick_update()))

            texts = [str(item.get("text") or "") for item in sent]
            self.assertTrue(any("📚 فكرة محفوظة" in text for text in texts), texts)
            self.assertFalse(any("🎛 لوحة نداء اليقظة" in text for text in texts), texts)
            buttons = [
                button
                for item in sent
                for row in ((item.get("reply_markup") or {}).get("inline_keyboard") or [])
                for button in row
            ]
            self.assertTrue(any(button.get("text") == "✅ استخدام هذا الموضوع" for button in buttons), buttons)

            state = panel.load_state(path)
            self.assertEqual(state.get("production_queue"), [])
            self.assertNotIn("production_target", state)
            active_session = str(state.get("active_research_session_id") or "")
            self.assertTrue(active_session)
            self.assertEqual(state["sessions"][active_session]["source"], "saved_suggestion")


if __name__ == "__main__":
    unittest.main()
