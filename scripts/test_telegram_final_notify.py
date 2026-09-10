from __future__ import annotations

import json
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


class SuccessFormattingTests(unittest.TestCase):
    def test_clean_success_is_green_and_reports_actual_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = root / "plan.json"
            request = root / "request.json"
            delivery = root / "delivery-manifest.json"
            plan.write_text(json.dumps({"topic": "x", "plan_source": "gemini"}), encoding="utf-8")
            request.write_text(json.dumps({"topic": "موضوع الحلقة"}), encoding="utf-8")
            delivery.write_text(
                json.dumps({"delivery_kind": "long_plus_shorts", "short_count": 2}),
                encoding="utf-8",
            )
            text = notify.build_success_message(
                run_number="114",
                elapsed_seconds=1457,
                plan_path=plan,
                delivery_path=delivery,
                output_root=root,
                request_path=request,
            )
        self.assertIn("✅ الإنتاج مكتمل · Run #114", text)
        self.assertIn("🎬 موضوع الحلقة", text)
        self.assertIn("الحلقة الطويلة + 2 Shorts جاهزة", text)
        self.assertIn("Quality Gates: Passed", text)
        self.assertNotIn("⚠️ الإنتاج مكتمل", text)
        self.assertNotIn("A/B/C", text)

    def test_fallback_success_is_warning_not_pure_green(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps({"topic": "موضوع", "plan_source": "product_proof_fallback"}),
                encoding="utf-8",
            )
            text = notify.build_success_message(
                run_number="115",
                elapsed_seconds=60,
                plan_path=plan,
                output_root=root,
            )
        self.assertIn("⚠️ الإنتاج مكتمل مع ملاحظة · Run #115", text)
        self.assertIn("fallback", text)
        self.assertNotIn("✅ الإنتاج مكتمل", text)
        self.assertIn("Quality Gates: Passed", text)

    def test_abc_is_only_claimed_when_three_thumbnail_candidates_exist(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            delivery = root / "delivery-manifest.json"
            delivery.write_text(
                json.dumps({"delivery_kind": "long_plus_shorts", "short_count": 3}),
                encoding="utf-8",
            )
            (root / "thumbnail-plan.json").write_text(
                json.dumps({"candidates": [{}, {}, {}]}),
                encoding="utf-8",
            )
            text = notify.build_success_message(
                run_number="116",
                elapsed_seconds=10,
                delivery_path=delivery,
                output_root=root,
            )
        self.assertIn("الحلقة الطويلة + 3 Shorts + عناوين/صور A/B/C جاهزة", text)

    def test_unknown_delivery_does_not_invent_shorts_or_abc(self) -> None:
        text = notify.build_success_message(run_number="117", elapsed_seconds=1)
        self.assertIn("الحزمة النهائية جاهزة", text)
        self.assertNotIn("Shorts", text)
        self.assertNotIn("A/B/C", text)


class UrlActionTests(unittest.TestCase):
    def test_failure_keyboard_uses_logs_url_only(self) -> None:
        keyboard = notify.terminal_url_keyboard(
            job_status="failure",
            run_url="https://github.com/o/r/actions/runs/10",
            results_url="https://github.com/o/r/releases/tag/video-10",
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(buttons, [{"text": "📋 GitHub Logs", "url": "https://github.com/o/r/actions/runs/10"}])
        self.assertFalse(any("callback_data" in button for button in buttons))

    def test_success_keyboard_prefers_results_then_github(self) -> None:
        keyboard = notify.terminal_url_keyboard(
            job_status="success",
            run_url="https://github.com/o/r/actions/runs/10",
            results_url="https://github.com/o/r/releases/tag/video-10",
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(buttons[0]["text"], "📦 عرض النتائج")
        self.assertEqual(buttons[1]["text"], "🔗 GitHub")
        self.assertTrue(all("url" in button for button in buttons))

    def test_terminal_keyboard_adds_details_only_when_message_context_is_bound(self) -> None:
        keyboard = notify.terminal_keyboard(
            job_status="failure",
            run_url="https://github.com/o/r/actions/runs/10",
            run_id="10",
            progress_message_id="42",
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(buttons[0]["callback_data"], "cmd:opsdetails-10-42")
        self.assertEqual(buttons[1]["text"], "📋 GitHub Logs")
        unbound = notify.terminal_keyboard(
            job_status="failure",
            run_url="https://github.com/o/r/actions/runs/10",
            run_id="10",
            progress_message_id="",
        )
        self.assertFalse(any("callback_data" in button for row in unbound["inline_keyboard"] for button in row))

    def test_release_url_exists_only_after_successful_release_step(self) -> None:
        base = {
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_NUMBER": "22",
        }
        self.assertEqual(notify._results_url({**base, "CREATE_RELEASE_OUTCOME": "skipped"}), "")
        self.assertEqual(
            notify._results_url({**base, "CREATE_RELEASE_OUTCOME": "success"}),
            "https://github.com/o/r/releases/tag/video-22",
        )

    def test_telegram_release_url_uses_exact_release_tag_override(self) -> None:
        env = {
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_NUMBER": "22",
            "CREATE_RELEASE_OUTCOME": "success",
            "ISCO_RELEASE_TAG_OVERRIDE": "video-telegram-req-abc",
        }
        self.assertEqual(
            notify._results_url(env),
            "https://github.com/o/r/releases/tag/video-telegram-req-abc",
        )


class DeliveryTests(unittest.TestCase):
    @staticmethod
    def _message(chat_id: int, message_id: int) -> dict:
        return {"ok": True, "result": {"chat": {"id": chat_id}, "message_id": message_id}}

    def test_delivery_edits_saved_lifecycle_message_with_keyboard(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return self._message(123, 42)

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="123",
                allowed_user_id="123",
                text="failure",
                progress_message_id="42",
                reply_markup={"inline_keyboard": [[{"text": "logs", "url": "https://example.com"}]]},
            )
        finally:
            notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "editMessageText")
        self.assertEqual(calls[0][1]["message_id"], "42")
        self.assertIn("reply_markup", calls[0][1])
        self.assertIn("inline_keyboard", json.loads(calls[0][1]["reply_markup"]))

    def test_delivery_sends_only_when_no_lifecycle_message_exists(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return self._message(123, 7)

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="123",
                allowed_user_id="123",
                text="failure",
            )
        finally:
            notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "sendMessage")

    def test_wrong_private_operator_blocks_before_network(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return self._message(123, 7)

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="123",
                allowed_user_id="456",
                text="failure",
            )
        finally:
            notify._telegram_request = original
        self.assertFalse(ok)
        self.assertEqual(calls, [])

    def test_ok_true_wrong_chat_is_not_terminal_success(self) -> None:
        calls = []

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return self._message(456, 7)

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="123",
                allowed_user_id="123",
                text="failure",
            )
        finally:
            notify._telegram_request = original
        self.assertFalse(ok)
        self.assertEqual(calls[0][0], "sendMessage")

    def test_untrusted_edit_falls_back_to_fresh_attested_send(self) -> None:
        calls = []
        responses = [self._message(123, 99), self._message(123, 100)]

        def fake_request(token, method, payload):
            calls.append((method, payload))
            return responses.pop(0)

        original = notify._telegram_request
        notify._telegram_request = fake_request
        try:
            ok = notify.deliver_terminal_message(
                token="tok",
                chat_id="123",
                allowed_user_id="123",
                text="failure",
                progress_message_id="42",
            )
        finally:
            notify._telegram_request = original
        self.assertTrue(ok)
        self.assertEqual([method for method, _ in calls], ["editMessageText", "sendMessage"])

    def test_allowed_user_id_reads_materialized_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            secret_dir = root / "isco-secrets"
            secret_dir.mkdir()
            (secret_dir / "telegram-allowed-user-id").write_text("123", encoding="utf-8")
            self.assertEqual(notify._allowed_user_id({}, root), "123")
            self.assertEqual(notify._allowed_user_id({"TELEGRAM_ALLOWED_USER_ID": "456"}, root), "456")


if __name__ == "__main__":
    unittest.main()
