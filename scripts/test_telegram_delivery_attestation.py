from __future__ import annotations

import unittest

from scripts.telegram_delivery_attestation import (
    TelegramTargetAttestationError,
    attest_configured_target,
    attest_message_response,
)


class TelegramDeliveryAttestationTests(unittest.TestCase):
    def test_private_target_must_match_authorized_operator(self) -> None:
        self.assertEqual(attest_configured_target(chat_id="123", allowed_user_id="123"), 123)
        with self.assertRaises(TelegramTargetAttestationError):
            attest_configured_target(chat_id="123", allowed_user_id="456")

    def test_private_target_without_independent_operator_is_rejected(self) -> None:
        with self.assertRaises(TelegramTargetAttestationError):
            attest_configured_target(chat_id="123", allowed_user_id="")

    def test_group_target_does_not_fake_user_id_equality(self) -> None:
        self.assertEqual(attest_configured_target(chat_id="-100123", allowed_user_id="456"), -100123)

    def test_send_response_binds_chat_and_message(self) -> None:
        body = {"ok": True, "result": {"message_id": 979, "chat": {"id": 123}}}
        self.assertEqual(attest_message_response(body, expected_chat_id="123"), 979)

    def test_ok_true_wrong_chat_is_rejected(self) -> None:
        body = {"ok": True, "result": {"message_id": 979, "chat": {"id": 456}}}
        with self.assertRaises(TelegramTargetAttestationError):
            attest_message_response(body, expected_chat_id="123")

    def test_missing_result_identity_is_rejected(self) -> None:
        for body in (
            {"ok": True},
            {"ok": True, "result": {}},
            {"ok": True, "result": {"message_id": 979, "chat": {}}},
        ):
            with self.subTest(body=body):
                with self.assertRaises(TelegramTargetAttestationError):
                    attest_message_response(body, expected_chat_id="123")

    def test_edit_response_cannot_switch_message_identity(self) -> None:
        body = {"ok": True, "result": {"message_id": 980, "chat": {"id": 123}}}
        with self.assertRaises(TelegramTargetAttestationError):
            attest_message_response(
                body,
                expected_chat_id="123",
                expected_message_id=979,
            )


if __name__ == "__main__":
    unittest.main()
