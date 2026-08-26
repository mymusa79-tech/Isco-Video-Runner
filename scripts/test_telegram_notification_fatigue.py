from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import telegram_final_notify as final_notify
from scripts import telegram_progress as progress


class TelegramNotificationFatigueCertificationTests(unittest.TestCase):
    def setUp(self) -> None:
        progress._state.update(
            {
                "token": "",
                "chat_id": "",
                "message_id": None,
                "completed": set(),
                "current_stage": None,
                "run_id": "",
                "run_number": "",
                "topic": "",
            }
        )

    def test_one_start_message_then_stage_updates_edit_in_place(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_request(method: str, payload: dict):
            calls.append((method, dict(payload)))
            if method == "sendMessage":
                return {"ok": True, "result": {"message_id": 42}}
            return {"ok": True, "result": True}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            token = root / "token"
            chat = root / "chat"
            request = root / "request.json"
            token.write_text("tok", encoding="utf-8")
            chat.write_text("777", encoding="utf-8")
            request.write_text(json.dumps({"topic": "موضوع"}), encoding="utf-8")
            env = {
                "TELEGRAM_BOT_TOKEN_FILE": str(token),
                "TELEGRAM_CHAT_ID_FILE": str(chat),
                "REQUEST_FILE": str(request),
                "RUNNER_TEMP": td,
                "GITHUB_RUN_ID": "123",
                "GITHUB_RUN_NUMBER": "112",
            }
            with patch.dict(os.environ, env, clear=False), patch.object(progress, "_telegram_request", side_effect=fake_request):
                progress.start_progress()
                for stage in ("planning", "voice", "visuals", "mux"):
                    progress.update_stage(stage)

        methods = [method for method, _ in calls]
        self.assertEqual(methods.count("sendMessage"), 1)
        self.assertEqual(methods.count("editMessageText"), 4)
        self.assertEqual(methods[0], "sendMessage")
        self.assertTrue(all(method == "editMessageText" for method in methods[1:]))

    def test_terminal_state_reuses_lifecycle_message_instead_of_adding_alert(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_request(token: str, method: str, payload: dict):
            calls.append((method, dict(payload)))
            return True

        with patch.object(final_notify, "_telegram_request", side_effect=fake_request):
            final_notify.deliver_terminal_message(
                token="tok",
                chat_id="777",
                text="✅ الإنتاج مكتمل",
                progress_message_id="42",
                reply_markup={"inline_keyboard": []},
            )
        self.assertEqual([method for method, _ in calls], ["editMessageText"])

    def test_missing_lifecycle_message_has_one_bounded_terminal_fallback(self) -> None:
        calls: list[tuple[str, dict]] = []

        def fake_request(token: str, method: str, payload: dict):
            calls.append((method, dict(payload)))
            return True

        with patch.object(final_notify, "_telegram_request", side_effect=fake_request):
            final_notify.deliver_terminal_message(
                token="tok",
                chat_id="777",
                text="❌ فشل الإنتاج",
                progress_message_id="",
            )
        self.assertEqual([method for method, _ in calls], ["sendMessage"])

    def test_v1_terminal_and_progress_surfaces_never_expose_retry(self) -> None:
        self.assertNotIn("retry", final_notify.ops_ui.V1_ACTIONS)
        failure = final_notify.build_failure_message(
            run_number="112",
            elapsed_seconds=10,
            env={"PRODUCE_VIDEO_OUTCOME": "failure"},
            runner_temp=Path("/path/that/does/not/exist"),
        )
        self.assertNotIn("🔁", failure)
        keyboard = final_notify.terminal_keyboard(
            job_status="failure",
            run_url="https://github.com/o/r/actions/runs/123",
            run_id="123",
            progress_message_id="42",
        )
        self.assertFalse(
            any("retry" in str(button).lower() for row in keyboard["inline_keyboard"] for button in row)
        )


if __name__ == "__main__":
    unittest.main()
