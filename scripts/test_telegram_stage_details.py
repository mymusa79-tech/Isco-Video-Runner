from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare" / "telegram-control-worker" / "observability-worker-v6.js"

sys.path.insert(0, str(ROOT / "scripts"))
import telegram_progress as progress  # noqa: E402


class TelegramStageDetailsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.worker = WORKER.read_text(encoding="utf-8")

    def test_active_worker_keeps_general_status_and_adds_opt_in_details(self) -> None:
        self.assertIn('`🧭 Production V4${value.run && value.run.run_number', self.worker)
        self.assertIn('{ text: "🔄 تحديث", callback_data: "cmd:status" }', self.worker)
        self.assertIn('text: "🧩 تفاصيل المراحل"', self.worker)
        self.assertIn('callback_data: `cmd:stage_details:${value.run.id}`', self.worker)
        self.assertIn('else await sendPanel(env, target, text, rows);', self.worker)
        self.assertIn('text: "📊 الحالة العامة"', self.worker)

    def test_detail_callback_is_bound_to_exact_canonical_run(self) -> None:
        self.assertIn('^cmd:stage_details:(\\d+)$', self.worker)
        self.assertIn('^cmd:stage_details_refresh:(\\d+)$', self.worker)
        self.assertIn('productionStateForRun(env, runId)', self.worker)
        self.assertIn('String(run.path || "") !== CANONICAL_PRODUCTION_PATH', self.worker)
        self.assertIn('String(value.run_id || "") !== String(run.id)', self.worker)

    def test_completed_planning_boundary_maps_to_editorial_qa_only_in_details(self) -> None:
        self.assertIn('function detailedInternalStage(progress)', self.worker)
        self.assertIn('completed.has("planning") ? "editorial_qa" : "planning"', self.worker)
        self.assertIn('["editorial_qa", "المراجعة التحريرية وQA"]', self.worker)
        self.assertIn('completed.has("mux") ? "final_qc" : "render"', self.worker)
        self.assertIn('["final_qc", "الفحص النهائي"]', self.worker)

    def test_details_are_read_only_and_do_not_add_retry_or_production_actions(self) -> None:
        self.assertIn('تفاصيل قراءة فقط؛ لا تغيّر Production أو أي Quality/Security Gate.', self.worker)
        self.assertNotIn('cmd:retry', self.worker)
        self.assertNotIn('telegram-production-request.yml', self.worker)

    def test_worker_syntax_is_valid_when_node_is_available(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is unavailable")
        subprocess.run([node, "--check", str(WORKER)], check=True, capture_output=True, text=True)


class TelegramProgressCompletionSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        progress._state["completed"] = set()
        progress._state["current_stage"] = "planning"

    def test_mark_stage_done_persists_completion_boundary_without_changing_card(self) -> None:
        with patch.object(progress, "_enqueue_progress_snapshot") as enqueue:
            progress.mark_stage_done("planning")
        self.assertEqual(progress._state["completed"], {"planning"})
        self.assertEqual(progress._state["current_stage"], "planning")
        enqueue.assert_called_once_with("planning")

    def test_invalid_stage_does_not_create_observability_snapshot(self) -> None:
        with patch.object(progress, "_enqueue_progress_snapshot") as enqueue:
            progress.mark_stage_done("not-a-stage")
        self.assertEqual(progress._state["completed"], set())
        enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
