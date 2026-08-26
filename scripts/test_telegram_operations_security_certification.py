from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from scripts import telegram_publish_gate as gate
from scripts import telegram_control_panel as panel


class TelegramOperationsSecurityCertificationTests(unittest.TestCase):
    @staticmethod
    def _callback(*, message_id: int = 42, data: str = "approve:run-1", user_id: int = 555, update_id: int = 1) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}",
                "data": data,
                "from": {"id": user_id},
                "message": {"message_id": message_id, "chat": {"id": 777}},
            },
        }

    def test_1_stale_message_button_is_rejected_without_side_effect(self) -> None:
        update = self._callback(message_id=999, data="approve:run-1")
        before = copy.deepcopy(update)
        with patch.object(gate, "is_authorized_user") as auth, patch.object(gate, "_telegram_api") as api:
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertIsNone(result)
        self.assertEqual(update, before)
        auth.assert_not_called()
        api.assert_not_called()

    def test_2_duplicate_authorized_press_has_one_effective_decision(self) -> None:
        updates = [
            self._callback(update_id=10, data="approve:run-1"),
            self._callback(update_id=11, data="approve:run-1"),
        ]
        with patch.object(gate, "_prime_offset", return_value=0), \
                patch.object(gate.time, "monotonic", side_effect=[0.0, 0.0]), \
                patch.object(gate, "_telegram_api", return_value=updates), \
                patch.object(gate, "_handle_update", return_value={"decision": "approved", "decided_by": 555, "decided_at": "t"}) as handle:
            result = gate.poll_for_decision("tok", 42, "run-1", timeout_seconds=1800)
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(handle.call_count, 1)
        self.assertEqual(handle.call_args.args[1]["update_id"], 10)

    def test_3_unauthorized_press_is_rejected_and_input_state_is_unchanged(self) -> None:
        update = self._callback(data="approve:run-1", user_id=999)
        before = copy.deepcopy(update)
        with patch.object(gate, "is_authorized_user", return_value=False), patch.object(gate, "_telegram_api") as api:
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertIsNone(result)
        self.assertEqual(update, before)
        api.assert_called_once()
        payload = api.call_args.kwargs["payload"]
        self.assertEqual(payload["show_alert"], "true")

        authorized, chat_id, user_id = panel._authorized_user(update, 555, "777")
        self.assertFalse(authorized)
        self.assertEqual(chat_id, 777)
        self.assertEqual(user_id, 999)
        self.assertEqual(update, before)

    def test_4_malformed_or_unknown_callback_causes_no_action(self) -> None:
        for data in ("", "approve", "unknown:run-1", "approve:run-1:extra"):
            with self.subTest(data=data):
                update = self._callback(data=data)
                before = copy.deepcopy(update)
                with patch.object(gate, "is_authorized_user") as auth, patch.object(gate, "_telegram_api") as api:
                    result = gate._handle_update("tok", update, 42, "run-1")
                self.assertIsNone(result)
                self.assertEqual(update, before)
                auth.assert_not_called()
                api.assert_not_called()

    def test_5_correct_action_with_wrong_run_id_causes_no_action(self) -> None:
        update = self._callback(data="approve:run-OTHER")
        before = copy.deepcopy(update)
        with patch.object(gate, "is_authorized_user") as auth, patch.object(gate, "_telegram_api") as api:
            result = gate._handle_update("tok", update, 42, "run-1")
        self.assertIsNone(result)
        self.assertEqual(update, before)
        auth.assert_not_called()
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
