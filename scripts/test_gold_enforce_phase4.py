from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from isco_video_agent.ai_budget import BudgetLedger
import scripts.gold_enforce_phase4 as phase4
import scripts.run_v3_voice as runner


class GoldEnforcePhase4Tests(unittest.TestCase):
    @staticmethod
    def _fake_acceptance() -> dict:
        return {
            "contract_id": "gold.packaging.acceptance.v2",
            "profile": "long_title_thumbnail_hypothesis_set",
            "decision": "pass",
        }

    def test_success_enforces_one_gold_critic_and_packaging_seal_before_state_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            (root / "final.mp4").write_bytes(b"immutable-render")
            history = Path(tmp) / "history.json"
            history.write_text(json.dumps({"productions": [{"id": 1}]}), encoding="utf-8")
            order: list[str] = []
            critic_kwargs: list[dict] = []
            fake_plan = type("Plan", (), {"format": "film"})()
            acceptance = self._fake_acceptance()

            def fake_critic(**kwargs):
                order.append("critic")
                critic_kwargs.append(kwargs)
                return {
                    "status": "pass",
                    "hard_blocks": [],
                    "model_review": {"status": "pass", "summary": "ok"},
                }

            def fake_seal(output_dir: Path, **_kwargs):
                order.append("seal")
                (Path(output_dir) / phase4.ACCEPTANCE_FILENAME).write_text(
                    json.dumps(acceptance), encoding="utf-8"
                )
                return acceptance

            def fake_mark(*args, **kwargs):
                order.append("accept")
                return True

            with patch.dict("os.environ", {"ISCO_HISTORY_PATH": str(history)}, clear=False), patch.object(
                phase4, "_output_key", return_value="output/test/final.mp4"
            ), patch.object(phase4, "_plan_from_json", return_value=fake_plan), patch.object(
                phase4, "build_budgeted_thumbnail_package", return_value={"status": "ready", "candidates": []}
            ), patch.object(phase4, "_augment_rights"), patch.object(
                phase4, "_run_final_critic", side_effect=fake_critic
            ), patch.object(phase4, "seal_gold_packaging_acceptance", side_effect=fake_seal), patch.object(
                phase4, "gold_packaging_acceptance_sha256", return_value="a" * 64
            ), patch.object(
                phase4, "mark_production_accepted", side_effect=fake_mark
            ) as mark, patch.object(phase4, "remove_production_record") as remove, patch.object(
                phase4, "_sync_state_snapshot"
            ) as sync:
                plan, critic, report = phase4.run_gold_enforce_phase4(
                    output_dir=root,
                    gemini="g",
                    pexels="p",
                    ledger=BudgetLedger("film", enforce=True),
                )

            self.assertEqual(order, ["critic", "seal", "accept"])
            self.assertEqual(len(critic_kwargs), 1)
            self.assertEqual(critic_kwargs[0]["release_mode"], "enforce")
            self.assertEqual(critic_kwargs[0]["task_prefix"], "GOLD_")
            self.assertEqual(critic_kwargs[0]["task_kind"], "GOLD_FINAL_CRITIC")
            mark.assert_called_once()
            remove.assert_not_called()
            sync.assert_not_called()
            self.assertEqual(plan, fake_plan)
            self.assertEqual(critic["status"], "pass")
            self.assertTrue(report["gold"]["accepted"])
            self.assertFalse(report["same_render"]["artifact_divergence"])
            self.assertEqual(report["release_authority"], "gold")
            self.assertTrue(report["packaging_acceptance"]["sealed_before_state_acceptance"])
            self.assertEqual(report["packaging_acceptance"]["certificate_sha256"], "a" * 64)
            self.assertEqual(report["packaging_acceptance"]["embedded_certificate"], acceptance)

    def test_packaging_seal_failure_cleans_history_and_never_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            (root / "final.mp4").write_bytes(b"immutable-render")
            with patch.object(phase4, "_output_key", return_value="output/test/final.mp4"), patch.object(
                phase4, "_plan_from_json", return_value=object()
            ), patch.object(
                phase4, "build_budgeted_thumbnail_package", return_value={"status": "ready", "candidates": []}
            ), patch.object(phase4, "_augment_rights"), patch.object(
                phase4,
                "_run_final_critic",
                return_value={"status": "pass", "hard_blocks": [], "model_review": {"summary": "ok"}},
            ), patch.object(
                phase4, "seal_gold_packaging_acceptance", side_effect=RuntimeError("seal blocked")
            ), patch.object(phase4, "mark_production_accepted") as mark, patch.object(
                phase4, "remove_production_record"
            ) as remove, patch.object(phase4, "_sync_state_snapshot") as sync:
                with self.assertRaisesRegex(RuntimeError, "seal blocked"):
                    phase4.run_gold_enforce_phase4(
                        output_dir=root,
                        gemini="g",
                        pexels="p",
                        ledger=BudgetLedger("film", enforce=True),
                    )
            mark.assert_not_called()
            remove.assert_called_once_with("output/test/final.mp4")
            sync.assert_called_once_with(root)

    def test_critic_failure_cleans_history_and_never_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            (root / "final.mp4").write_bytes(b"immutable-render")
            history = Path(tmp) / "history.json"
            history.write_text("{}", encoding="utf-8")
            fake_plan = object()
            with patch.dict("os.environ", {"ISCO_HISTORY_PATH": str(history)}, clear=False), patch.object(
                phase4, "_output_key", return_value="output/test/final.mp4"
            ), patch.object(phase4, "_plan_from_json", return_value=fake_plan), patch.object(
                phase4, "build_budgeted_thumbnail_package", return_value={"status": "ready", "candidates": []}
            ), patch.object(phase4, "_augment_rights"), patch.object(
                phase4, "_run_final_critic", side_effect=RuntimeError("blocked")
            ), patch.object(phase4, "mark_production_accepted") as mark, patch.object(
                phase4, "remove_production_record"
            ) as remove, patch.object(phase4, "_sync_state_snapshot") as sync:
                with self.assertRaisesRegex(RuntimeError, "blocked"):
                    phase4.run_gold_enforce_phase4(
                        output_dir=root,
                        gemini="g",
                        pexels="p",
                        ledger=BudgetLedger("film", enforce=True),
                    )
            mark.assert_not_called()
            remove.assert_called_once_with("output/test/final.mp4")
            sync.assert_called_once_with(root)
            report = json.loads((root / "gold-enforce-report.json").read_text(encoding="utf-8"))
            self.assertFalse(report["gold"]["accepted"])
            self.assertTrue(report["state_observation"]["failure_cleanup_expected"])

    def test_failure_report_never_persists_raw_exception_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            (root / "final.mp4").write_bytes(b"immutable-render")
            marker = "https://provider.invalid/?key=SHOULD_NOT_APPEAR"
            with patch.object(phase4, "_output_key", return_value="output/test/final.mp4"), patch.object(
                phase4, "_plan_from_json", return_value=object()
            ), patch.object(
                phase4, "build_budgeted_thumbnail_package", side_effect=RuntimeError(marker)
            ), patch.object(phase4, "remove_production_record"), patch.object(
                phase4, "_sync_state_snapshot"
            ):
                with self.assertRaises(RuntimeError):
                    phase4.run_gold_enforce_phase4(
                        output_dir=root,
                        gemini="dummy",
                        pexels="dummy",
                        ledger=BudgetLedger("film", enforce=True),
                    )
            report_text = (root / "gold-enforce-report.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertNotIn(marker, report_text)
            self.assertEqual(report["error"], {"type": "RuntimeError"})

    def test_final_video_mutation_is_detected_inside_cleanup_boundary_before_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "output"
            root.mkdir()
            final = root / "final.mp4"
            final.write_bytes(b"before")
            fake_plan = object()

            def mutating_critic(**_kwargs):
                final.write_bytes(b"after")
                return {
                    "status": "pass",
                    "hard_blocks": [],
                    "model_review": {"summary": "ok"},
                }

            with patch.object(phase4, "_output_key", return_value="output/test/final.mp4"), patch.object(
                phase4, "_plan_from_json", return_value=fake_plan
            ), patch.object(
                phase4, "build_budgeted_thumbnail_package", return_value={"status": "ready", "candidates": []}
            ), patch.object(phase4, "_augment_rights"), patch.object(
                phase4, "_run_final_critic", side_effect=mutating_critic
            ), patch.object(phase4, "mark_production_accepted") as mark, patch.object(
                phase4, "remove_production_record"
            ) as remove, patch.object(phase4, "_sync_state_snapshot") as sync:
                with self.assertRaisesRegex(RuntimeError, "final.mp4 mutation before state acceptance"):
                    phase4.run_gold_enforce_phase4(
                        output_dir=root,
                        gemini="g",
                        pexels="p",
                        ledger=BudgetLedger("film", enforce=True),
                    )
            mark.assert_not_called()
            remove.assert_called_once()
            sync.assert_called_once()


class Phase4RunnerContracts(unittest.TestCase):
    def test_runner_has_one_core_render_then_one_gold_enforcer_before_manifest_and_analytics(self) -> None:
        source = inspect.getsource(runner.main)
        self.assertEqual(source.count("orchestrator.produce("), 1)
        self.assertEqual(source.count("run_gold_enforce_phase4("), 1)
        self.assertNotIn("run_gold_single_evaluator_phase3(", source)
        order = [
            source.index("orchestrator.produce("),
            source.index("run_gold_enforce_phase4("),
            source.index("_write_production_manifest("),
            source.index("observe_post_acceptance_analytics("),
        ]
        self.assertEqual(order, sorted(order))
        self.assertIn('"release_authority": "gold_enforced"', inspect.getsource(runner._write_production_manifest))


if __name__ == "__main__":
    unittest.main()
