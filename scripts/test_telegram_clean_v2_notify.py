from __future__ import annotations

import unittest

from scripts.telegram_clean_v2_notify import (
    artifact_delivery_text,
    build_message_payload,
    bundle_blocked_text,
    bundle_summary_text,
    current_stage,
    failure_guidance,
    milestone_messages,
    short_failure_reason,
    runtime_status_payload,
    started_text,
    terminal_text,
    workflow_watchdog_text,
)


class TelegramCleanV2NotifyTests(unittest.TestCase):
    def test_milestones_are_external_projection_of_manifest(self):
        manifest = {
            "stages": [
                {"name": "brief", "status": "pass"},
                {"name": "planning", "status": "pass"},
                {"name": "narrative_identity", "status": "pass"},
                {"name": "script", "status": "pass"},
                {"name": "voice", "status": "running"},
            ]
        }
        self.assertEqual(
            [name for name, _ in milestone_messages(manifest, set(), kind="long")],
            ["planning", "script"],
        )
        self.assertEqual(
            [name for name, _ in milestone_messages(manifest, {"planning"}, kind="long")],
            ["script"],
        )
        self.assertEqual(current_stage(manifest), "voice")

    def test_failure_reason_uses_sanitized_manifest_fields(self):
        manifest = {
            "status": "failed",
            "stages": [
                {"name": "planning", "status": "pass"},
                {
                    "name": "script",
                    "status": "failed",
                    "error_type": "RuntimeError",
                    "failure_classification": "infrastructure",
                },
            ],
        }
        self.assertEqual(
            short_failure_reason(manifest, "failure"),
            "RuntimeError · infrastructure",
        )

    def test_terminal_success_requires_workflow_and_manifest_success(self):
        manifest = {
            "status": "pass",
            "topic": "موضوع تجريبي",
            "stages": [{"name": "final_master_qc", "status": "pass"}],
        }
        success = terminal_text(
            manifest=manifest,
            job_status="success",
            kind="short",
            run_url="https://github.example/run/1",
        )
        failed_verification = terminal_text(
            manifest=manifest,
            job_status="failure",
            kind="short",
            run_url="https://github.example/run/1",
        )
        self.assertIn("نجح", success)
        self.assertIn("فشل", failed_verification)
        self.assertIn("فشل تحقق Workflow", failed_verification)

    def test_watchdog_explains_failure_before_pipeline_stages(self):
        text = workflow_watchdog_text(scope="short", run_url="https://github.example/run/2")
        self.assertIn("تعذر إكمال الشورت", text)
        self.assertIn("الخطوة التالية", text)
        self.assertIn("https://github.example/run/2", text)

    def test_failure_guidance_turns_infrastructure_into_user_action(self):
        manifest = {
            "status": "failed",
            "stages": [
                {
                    "name": "script",
                    "status": "failed",
                    "error_type": "RuntimeError",
                    "failure_classification": "infrastructure",
                }
            ],
        }
        guidance = failure_guidance(manifest, "failure")
        self.assertIn("انتظر قليلًا", guidance)
        self.assertIn("أعد المحاولة", guidance)

    def test_started_message_confirms_real_workflow_start(self):
        text = started_text(scope="bundle", topic="موضوع تجريبي", run_url="https://github.example/run/3")
        self.assertIn("بدأ الإنتاج فعليًا", text)
        self.assertIn("طويل", text)
        self.assertIn("شورت", text)
        self.assertIn("موضوع تجريبي", text)

    def test_stage_messages_are_arabic_and_identify_content_type(self):
        manifest = {
            "stages": [
                {"name": "planning", "status": "pass"},
                {"name": "script", "status": "pass"},
            ]
        }
        messages = dict(milestone_messages(manifest, set(), kind="short"))
        self.assertIn("⚡ الشورت", messages["planning"])
        self.assertIn("1/6 التخطيط", messages["planning"])
        self.assertIn("2/6 النص", messages["script"])

    def test_bundle_summary_reports_both_outputs(self):
        text = bundle_summary_text(topic="موضوع", run_url="https://github.example/run/4")
        self.assertIn("الفيديو الطويل: مكتمل", text)
        self.assertIn("الشورت: مكتمل", text)
        self.assertIn("اكتملت الحزمة كاملة", text)

    def test_bundle_long_failure_explains_short_was_not_started(self):
        text = bundle_blocked_text(topic="موضوع", run_url="https://github.example/run/5")
        self.assertIn("فشل الفيديو الطويل", text)
        self.assertIn("الشورت لم يبدأ", text)
        self.assertIn("أعد محاولة الفيديو الطويل", text)

    def test_artifact_delivery_uses_direct_final_video_button(self):
        payload = build_message_payload(
            artifact_delivery_text(scope="short", topic="موضوع"),
            chat_id="123",
            button_text="🎥 فتح الفيديو النهائي",
            button_url="https://github.example/artifacts/7",
        )
        button = payload["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "🎥 فتح الفيديو النهائي")
        self.assertEqual(button["url"], "https://github.example/artifacts/7")
        self.assertIn("جاهز للتسليم", payload["text"])

    def test_runtime_status_payload_tracks_real_last_stage(self):
        payload = runtime_status_payload(
            active=True,
            scope="bundle",
            kind="short",
            topic="موضوع",
            stage="⚡ الشورت · 3/6 الصوت ✅",
            run_url="https://github.example/run/6",
        )
        self.assertTrue(payload["active"])
        self.assertEqual(payload["kind"], "short")
        self.assertIn("3/6 الصوت", payload["stage"])


if __name__ == "__main__":
    unittest.main()
