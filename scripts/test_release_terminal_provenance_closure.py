from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import run_v3_voice
from scripts import telegram_final_notify as notify


class ProductionManifestReleaseIdentityTests(unittest.TestCase):
    def test_manual_v4_manifest_keeps_default_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"final")
            with patch.dict(
                os.environ,
                {"GITHUB_RUN_NUMBER": "77", "ISCO_RELEASE_TAG_OVERRIDE": ""},
                clear=False,
            ):
                manifest = run_v3_voice._write_production_manifest(
                    root,
                    production_id="v4:test:1",
                    fmt="film",
                )
        self.assertEqual(manifest["release_tag"], "video-77")

    def test_telegram_v4_manifest_uses_exact_release_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"final")
            with patch.dict(
                os.environ,
                {
                    "GITHUB_RUN_NUMBER": "77",
                    "ISCO_RELEASE_TAG_OVERRIDE": "video-telegram-req-abc",
                },
                clear=False,
            ):
                manifest = run_v3_voice._write_production_manifest(
                    root,
                    production_id="v4:test:1",
                    fmt="film",
                )
        self.assertEqual(manifest["release_tag"], "video-telegram-req-abc")


class TerminalDeliveryBoundaryTests(unittest.TestCase):
    def test_successful_release_dominates_later_job_failure(self) -> None:
        env = {
            "JOB_STATUS": "failure",
            "CREATE_RELEASE_OUTCOME": "success",
            "PERSIST_STATE_OUTCOME": "failure",
        }
        self.assertEqual(notify.terminal_delivery_status(env), "released_degraded")
        self.assertIn("لا تعِد الإنتاج", notify.released_degraded_warning(env))

    def test_release_failure_remains_failure(self) -> None:
        env = {
            "JOB_STATUS": "failure",
            "CREATE_RELEASE_OUTCOME": "failure",
        }
        self.assertEqual(notify.terminal_delivery_status(env), "failure")

    def test_clean_job_success_remains_success(self) -> None:
        env = {
            "JOB_STATUS": "success",
            "CREATE_RELEASE_OUTCOME": "success",
        }
        self.assertEqual(notify.terminal_delivery_status(env), "success")

    def test_degraded_release_keyboard_keeps_results_visible(self) -> None:
        keyboard = notify.terminal_keyboard(
            job_status="released_degraded",
            run_url="https://github.com/o/r/actions/runs/10",
            results_url="https://github.com/o/r/releases/tag/video-telegram-req-abc",
        )
        buttons = [button for row in keyboard["inline_keyboard"] for button in row]
        self.assertEqual(buttons[0]["text"], "📦 عرض النتائج")
        self.assertEqual(buttons[1]["text"], "🔗 GitHub")

    def test_degraded_success_copy_is_warning_not_failure(self) -> None:
        text = notify.build_success_message(
            run_number="120",
            elapsed_seconds=60,
            additional_warning="تم إنشاء GitHub Release بنجاح. لا تعِد الإنتاج.",
        )
        self.assertIn("⚠️ الإنتاج مكتمل مع ملاحظة", text)
        self.assertIn("لا تعِد الإنتاج", text)
        self.assertNotIn("❌ فشل الإنتاج", text)


if __name__ == "__main__":
    unittest.main()
