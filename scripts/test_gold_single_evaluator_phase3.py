from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from isco_video_agent.ai_budget import BudgetLedger
import scripts.gold_single_evaluator_phase3 as phase3
import scripts.run_v3_voice as runner


class Phase3SingleEvaluatorTests(unittest.TestCase):
    def _write_release_inputs(self, root: Path) -> None:
        (root / "final.mp4").write_bytes(b"one-render")
        (root / "plan.json").write_text(
            json.dumps(
                {
                    "topic": "topic",
                    "pillar": "understand",
                    "format": "film",
                    "hook": "hook",
                    "title_options": ["title"],
                    "thumbnail_concepts": ["thumb"],
                    "sections": [],
                    "cta": "cta",
                    "closing_payoff": "close",
                }
            ),
            encoding="utf-8",
        )
        (root / "quality-final.json").write_text(json.dumps({"duration_ok": True}), encoding="utf-8")
        (root / "visual-audit.json").write_text(json.dumps([]), encoding="utf-8")
        (root / "rights-manifest.json").write_text(
            json.dumps({"visuals": [{"provider": "pexels"}]}), encoding="utf-8"
        )
        (root / "monetization-check.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")

    def test_one_gold_critic_owns_canonical_observer_report_without_mutating_release_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            self._write_release_inputs(root)
            history = Path(tmp) / "history.json"
            history.write_text(json.dumps({"productions": [{"id": 1}]}), encoding="utf-8")
            rights_before = (root / "rights-manifest.json").read_bytes()
            package = {"status": "ready", "candidates": []}
            calls: list[dict] = []

            def fake_critic(**kwargs):
                calls.append(kwargs)
                critic = {
                    "status": "pass",
                    "mode": "observe_only",
                    "observation_status": "ok",
                    "would_block_if_enforced": False,
                    "hard_blocks": [],
                    "model_review": {"status": "pass"},
                }
                report_dir = Path(kwargs["report_dir"])
                (report_dir / "final-critic.json").write_text(json.dumps(critic), encoding="utf-8")
                (report_dir / "opening-visual-audit.json").write_text(
                    json.dumps({"status": "pass"}), encoding="utf-8"
                )
                return critic

            with patch.object(phase3, "history_path", return_value=history), patch.object(
                phase3, "build_budgeted_thumbnail_package", return_value=package
            ), patch.object(phase3, "_run_final_critic", side_effect=fake_critic):
                critic, report = phase3.run_gold_single_evaluator_phase3(
                    output_dir=root,
                    gemini="g",
                    pexels="p",
                    ledger=BudgetLedger("film", enforce=True),
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["release_mode"], "observe_only")
            self.assertEqual(calls[0]["task_prefix"], "GOLD_")
            self.assertEqual(calls[0]["task_kind"], "GOLD_FINAL_CRITIC")
            self.assertEqual(Path(calls[0]["report_dir"]), root)
            self.assertTrue(report["single_gold_evaluator"])
            self.assertFalse(report["legacy_v4"]["evaluator_run"])
            self.assertEqual(report["release_authority"], "legacy_v4")
            self.assertFalse(report["same_render"]["artifact_divergence"])
            self.assertFalse(report["rights_observation"]["canonical_rights_mutation_detected"])
            self.assertFalse(report["state_observation"]["state_mutation_detected"])
            self.assertEqual((root / "rights-manifest.json").read_bytes(), rights_before)
            self.assertEqual(json.loads((root / "final-critic.json").read_text())["status"], "pass")
            self.assertEqual(critic["status"], "pass")

    def test_runner_source_has_one_core_render_and_no_legacy_critic_call(self) -> None:
        source = inspect.getsource(runner.main)
        self.assertEqual(source.count("orchestrator.produce("), 1)
        self.assertEqual(source.count("run_gold_single_evaluator_phase3("), 1)
        self.assertNotIn("_run_final_critic(", source)
        self.assertLess(source.index("orchestrator.produce("), source.index("run_gold_single_evaluator_phase3("))


if __name__ == "__main__":
    unittest.main()
