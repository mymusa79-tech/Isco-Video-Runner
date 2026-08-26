from __future__ import annotations

import unittest

from scripts import telegram_operations_ui as ui


class TelegramOperationsVocabularyTests(unittest.TestCase):
    def test_exact_four_severity_levels_are_locked(self) -> None:
        self.assertEqual(
            ui.SEVERITIES,
            frozenset({"INFO", "SUCCESS", "WARNING", "ACTION"}),
        )

    def test_visual_status_vocabulary_is_stable(self) -> None:
        self.assertEqual(ui.STATUS_EMOJI["running"], "🔵")
        self.assertEqual(ui.STATUS_EMOJI["success"], "✅")
        self.assertEqual(ui.STATUS_EMOJI["warning"], "⚠️")
        self.assertEqual(ui.STATUS_EMOJI["failed"], "❌")
        self.assertEqual(ui.STATUS_EMOJI["action_required"], "🟠")
        self.assertEqual(ui.STATUS_EMOJI["held"], "⏸️")

    def test_retry_is_explicitly_absent_from_v1_actions(self) -> None:
        self.assertNotIn("retry", ui.V1_ACTIONS)

    def test_contract_has_the_approved_shared_fields(self) -> None:
        contract = ui.TelegramMessageContract(
            status="running",
            severity=ui.SEVERITY_INFO,
            headline="بدأ الإنتاج",
            summary="لا يحتاج أي إجراء.",
            stage="planning",
            reason="",
            run_id="123",
            actions=(ui.ACTION_VIEW_GITHUB,),
        )
        self.assertEqual(
            list(ui.contract_dict(contract)),
            ["status", "severity", "headline", "summary", "stage", "reason", "run_id", "actions"],
        )

    def test_unknown_severity_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            ui.TelegramMessageContract(
                status="running",
                severity="DEBUG",
                headline="x",
            )

    def test_unknown_v1_action_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            ui.TelegramMessageContract(
                status="failed",
                severity=ui.SEVERITY_ACTION,
                headline="x",
                actions=("retry",),
            )


class TelegramButtonContractTests(unittest.TestCase):
    def test_url_button_requires_absolute_http_url(self) -> None:
        with self.assertRaises(ValueError):
            ui.url_button("GitHub", "/relative")

    def test_callback_limit_is_64_bytes(self) -> None:
        self.assertEqual(ui.callback_button("x", "a" * 64)["callback_data"], "a" * 64)
        with self.assertRaises(ValueError):
            ui.callback_button("x", "a" * 65)

    def test_inline_keyboard_omits_empty_rows(self) -> None:
        keyboard = ui.inline_keyboard(
            [
                [],
                [ui.url_button("GitHub", "https://github.com/example/repo")],
            ]
        )
        self.assertEqual(len(keyboard["inline_keyboard"]), 1)


if __name__ == "__main__":
    unittest.main()
