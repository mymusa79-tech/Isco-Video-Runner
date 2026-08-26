from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import telegram_final_notify as notify


class FailureFormattingTests(unittest.TestCase):
    def test_detects_first_failed_stage_in_workflow_order(self) -> None:
        env = {
            "VERIFY_PROVIDERS_OUTCOME": "failure",
            "PRODUCE_VIDEO_OUTCOME": "failure",
        }
        self.assertEqual(notify.detect_failure_stage(env), "Provider Readiness")

    def test_compact_reason_removes_exception_class_without_inventing_cause(self) -> None:
        self.assertEqual(
            notify.compact_failure_reason("RuntimeError: provider unavailable"),
            "provider unavailable",
        )

    def test_reads_latest_relevant_failure_line(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "production-step.log").write_text(
                "start\nValueError: old\nok\nRuntimeError: final reason\n",
                encoding="utf-8",
            )
            self.assertEqual(notify.read_failure_reason(root, "الإنتاج"), "final reason")

    def test_final_review_prefers_its_own_log(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final-review-step.log").write_text("RuntimeError: review failed\n", encoding="utf-8")
            (root / "production-step.log").write_text("RuntimeError: production failed\n", encoding="utf-8")
            self.assertEqual(notify.read_failure_reason(root, "Final Review"), "review failed")

    def test_failure_message_separates_reason_and_impact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "production-step.log").write_text("RuntimeError: visual gate blocked\n", encoding="utf-8")
            text = notify.build_failure_message(
                run_number="113",
                elapsed_seconds=521,
                env={"PRODUCE_VIDEO_OUTCOME": "failure"},
                runner_temp=root,
            )
        self.assertIn("❌ فشل الإنتاج · الإنتاج · Run #113", text)
        self.assertIn("السبب:", text)
        self.assertIn("visual gate blocked", text)
        self.assertIn("الأثر:", text)
        self.assertIn("8د 41ث", text)
        self.assertNotIn("🔁", text)


class DeliveryTests(unittest.TestCase):
    def test_delivery_edits_saved_lifecycle_message(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return True

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="chat",
                text="failure",
                progress_message_id="42",
            )
        finally:
            notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "editMessageText")
        self.assertEqual(calls[0][1]["message_id"], "42")

    def test_delivery_sends_only_when_no_lifecycle_message_exists(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return True

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(token="tok", chat_id="chat", text="failure")
        finally:
            notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "sendMessage")


if __name__ == "__main__":
    unittest.main()
