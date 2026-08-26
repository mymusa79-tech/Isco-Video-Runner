from __future__ import annotations

import unittest
from datetime import datetime, timezone

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


class TelegramFreshnessTests(unittest.TestCase):
    def test_freshness_line_is_rendered_in_oman_time(self) -> None:
        observed = datetime(2026, 8, 26, 20, 37, tzinfo=timezone.utc)
        self.assertEqual(ui.freshness_line(observed), "🕒 آخر تحقق: 00:37 · عُمان")

    def test_all_lifecycle_surfaces_expose_last_verified_time(self) -> None:
        observed = datetime(2026, 8, 26, 20, 37, tzinfo=timezone.utc)
        messages = (
            ui.render_progress_text(run_number="118", current_stage="planning", observed_at=observed),
            ui.render_failure_text(run_number="118", stage="الإنتاج", observed_at=observed),
            ui.render_success_text(run_number="118", observed_at=observed),
        )
        for message in messages:
            self.assertIn("🕒 آخر تحقق: 00:37 · عُمان", message)

    def test_existing_progress_contract_is_preserved_with_freshness_footer(self) -> None:
        observed = datetime(2026, 8, 26, 20, 37, tzinfo=timezone.utc)
        text = ui.render_progress_text(
            run_number="118",
            topic="موضوع الحلقة",
            current_stage="voice",
            completed={"planning"},
            observed_at=observed,
        )
        self.assertIn("🔵 الإنتاج جارٍ · Run #118", text)
        self.assertIn("التخطيط ✅", text)
        self.assertIn("الصوت 🔵", text)
        self.assertTrue(text.endswith("🕒 آخر تحقق: 00:37 · عُمان"))


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

    def test_details_callback_binds_run_and_message(self) -> None:
        data = ui.operations_callback_data(ui.ACTION_DETAILS, "32933244427", "42")
        self.assertEqual(data, "cmd:opsdetails-32933244427-42")
        self.assertEqual(
            ui.parse_operations_command("opsdetails-32933244427-42"),
            (ui.ACTION_DETAILS, "32933244427", "42"),
        )

    def test_compact_callback_binds_run_and_message(self) -> None:
        data = ui.operations_callback_data(ui.ACTION_COMPACT, "32933244427", "42")
        self.assertEqual(data, "cmd:opscompact-32933244427-42")
        self.assertEqual(
            ui.parse_operations_command("opscompact-32933244427-42"),
            (ui.ACTION_COMPACT, "32933244427", "42"),
        )

    def test_malformed_toggle_fails_closed(self) -> None:
        self.assertIsNone(ui.parse_operations_command("opsdetails-not-a-run-42"))
        self.assertIsNone(ui.parse_operations_command("opsdetails-123"))
        self.assertIsNone(ui.parse_operations_command("opsdetails-0-42"))
        with self.assertRaises(ValueError):
            ui.operations_callback_data(ui.ACTION_DETAILS, "bad", "42")


if __name__ == "__main__":
    unittest.main()
