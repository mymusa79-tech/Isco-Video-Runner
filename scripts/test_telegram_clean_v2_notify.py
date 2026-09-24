from __future__ import annotations

import unittest

from scripts.telegram_clean_v2_notify import (
    current_stage,
    milestone_messages,
    short_failure_reason,
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
            [name for name, _ in milestone_messages(manifest, set())],
            ["planning", "script"],
        )
        self.assertEqual(
            [name for name, _ in milestone_messages(manifest, {"planning"})],
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


if __name__ == "__main__":
    unittest.main()
