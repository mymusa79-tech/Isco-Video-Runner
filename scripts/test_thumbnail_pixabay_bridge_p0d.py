from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.ai_budget import BudgetLedger
import scripts.gold_enforce_phase4 as phase4
import scripts.gold_thumbnail_budget as thumb_budget
import scripts.run_v3_voice as runner


class GoldPixabayBridgeTests(unittest.TestCase):
    def test_budget_adapter_forwards_pixabay_key_to_canonical_builder(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        with patch.object(thumbnail, "build_thumbnail_package", return_value={"status": "ready"}) as builder:
            result = thumb_budget.build_budgeted_thumbnail_package(
                gemini_key="g",
                pexels_key="p",
                pixabay_key="x",
                plan=object(),
                output_dir=Path("out"),
                model="model",
                ledger=ledger,
            )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(builder.call_args.kwargs["pixabay_key"], "x")
        self.assertEqual(builder.call_args.kwargs["pexels_key"], "p")

    def test_phase4_forwards_in_process_pixabay_key_without_changing_finalizer_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"same-render")
            fake_plan = type("Plan", (), {"format": "film"})()
            ledger = BudgetLedger("film", enforce=True)
            acceptance = {
                "contract_id": "gold.packaging.acceptance.v2",
                "profile": "long_title_thumbnail_hypothesis_set",
                "decision": "pass",
            }

            def fake_finalize(**kwargs):
                package = kwargs["build_thumbnail_package"](
                    gemini_key="g",
                    pexels_key="p",
                    plan=fake_plan,
                    output_dir=root,
                    model="model",
                )
                self.assertEqual(package["status"], "ready")
                critic = kwargs["run_final_critic"]()
                return fake_plan, critic

            def fake_seal(output_dir: Path, **_kwargs):
                (Path(output_dir) / phase4.ACCEPTANCE_FILENAME).write_text(
                    json.dumps(acceptance), encoding="utf-8"
                )
                return acceptance

            with patch.object(phase4, "_output_key", return_value="output/test/final.mp4"), patch.object(
                phase4, "finalize_gold_output", side_effect=fake_finalize
            ), patch.object(
                phase4, "build_budgeted_thumbnail_package", return_value={"status": "ready"}
            ) as builder, patch.object(
                phase4,
                "_run_final_critic",
                return_value={"status": "pass", "hard_blocks": [], "model_review": {"summary": "ok"}},
            ), patch.object(
                phase4, "seal_gold_packaging_acceptance", side_effect=fake_seal
            ), patch.object(
                phase4, "gold_packaging_acceptance_sha256", return_value="a" * 64
            ):
                plan, critic, report = phase4.run_gold_enforce_phase4(
                    output_dir=root,
                    gemini="g",
                    pexels="p",
                    pixabay="x",
                    ledger=ledger,
                )

            self.assertIs(plan, fake_plan)
            self.assertEqual(critic["status"], "pass")
            self.assertTrue(report["gold"]["accepted"])
            self.assertEqual(report["packaging_acceptance"]["certificate_sha256"], "a" * 64)
            self.assertEqual(builder.call_args.kwargs["pixabay_key"], "x")
            self.assertIs(builder.call_args.kwargs["ledger"], ledger)

    def test_runner_retains_pixabay_once_and_passes_it_to_gold(self) -> None:
        source = inspect.getsource(runner.main)
        self.assertIn('pixabay = secret("PIXABAY_API_KEY")', source)
        self.assertIn('os.environ["PIXABAY_API_KEY"] = pixabay', source)
        self.assertIn("pixabay=pixabay", source)
        self.assertEqual(source.count('secret("PIXABAY_API_KEY")'), 1)


if __name__ == "__main__":
    unittest.main()
