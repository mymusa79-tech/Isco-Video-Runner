from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import gold_enforce_phase4 as gold


class GoldViewerQualityPreAcceptanceV1Tests(unittest.TestCase):
    def _root(self) -> Path:
        root = Path(tempfile.mkdtemp(prefix="gold-viewer-preacceptance-"))
        (root / "final.mp4").write_bytes(b"same-final")
        (root / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
        return root

    def test_order_is_critic_then_viewer_then_packaging_then_state_acceptance(self) -> None:
        root = self._root()
        order: list[str] = []
        acceptance_file = root / gold.ACCEPTANCE_FILENAME

        def fake_finalize(**kwargs):
            plan = SimpleNamespace(format="moment")
            critic = kwargs["run_final_critic"](
                output_dir=root,
                plan=plan,
                gemini="g",
                content_model="m",
            )
            order.append("state_acceptance")
            return plan, critic

        def fake_critic(**kwargs):
            order.append("critic")
            return {"status": "pass", "hard_blocks": []}

        def fake_viewer(*args, **kwargs):
            order.append("viewer")
            report = {
                "contract_id": "viewer-quality.v1",
                "verdict": "pass",
                "viewer_score_10": 9.0,
                "minimum_viewer_score_10": 8.5,
                "release_profile": "standalone_short",
            }
            (root / "viewer-quality-contract.json").write_text(json.dumps(report), encoding="utf-8")
            return report

        def fake_seal(*args, **kwargs):
            order.append("packaging")
            acceptance_file.write_text("{}", encoding="utf-8")
            return {"contract_id": "gold.packaging", "profile": "test"}

        with patch.object(gold, "finalize_gold_output", side_effect=fake_finalize), patch.object(
            gold, "_run_final_critic", side_effect=fake_critic
        ), patch.object(
            gold, "enforce_viewer_quality_contract", side_effect=fake_viewer
        ), patch.object(
            gold, "seal_gold_packaging_acceptance", side_effect=fake_seal
        ), patch.object(
            gold, "gold_packaging_acceptance_sha256", return_value="cert"
        ), patch.object(
            gold, "_provider_attempt_total", return_value=0
        ), patch.object(
            gold, "build_budgeted_thumbnail_package", return_value={}
        ):
            _, _, report = gold.run_gold_enforce_phase4(
                output_dir=root,
                gemini="g",
                pexels="p",
                pixabay=None,
                ledger=object(),
            )

        self.assertEqual(order, ["critic", "viewer", "packaging", "state_acceptance"])
        self.assertTrue(report["viewer_quality"]["evaluated_before_packaging_and_state_acceptance"])
        self.assertTrue(report["packaging_acceptance"]["sealed_after_viewer_quality"])
        self.assertTrue(report["state_observation"]["acceptance_is_terminal_mutation"])

    def test_viewer_block_prevents_packaging_and_state_acceptance(self) -> None:
        root = self._root()
        order: list[str] = []

        def fake_finalize(**kwargs):
            plan = SimpleNamespace(format="moment")
            try:
                kwargs["run_final_critic"](
                    output_dir=root,
                    plan=plan,
                    gemini="g",
                    content_model="m",
                )
            except Exception:
                raise
            order.append("state_acceptance")
            return plan, {"status": "pass", "hard_blocks": []}

        def fake_critic(**kwargs):
            order.append("critic")
            return {"status": "pass", "hard_blocks": []}

        def fake_viewer(*args, **kwargs):
            order.append("viewer")
            report = {
                "contract_id": "viewer-quality.v1",
                "verdict": "block",
                "viewer_score_10": 8.1,
                "minimum_viewer_score_10": 8.5,
                "release_profile": "standalone_short",
            }
            (root / "viewer-quality-contract.json").write_text(json.dumps(report), encoding="utf-8")
            raise RuntimeError("Viewer Quality Contract V1 blocked release")

        with patch.object(gold, "finalize_gold_output", side_effect=fake_finalize), patch.object(
            gold, "_run_final_critic", side_effect=fake_critic
        ), patch.object(
            gold, "enforce_viewer_quality_contract", side_effect=fake_viewer
        ), patch.object(
            gold, "seal_gold_packaging_acceptance"
        ) as seal, patch.object(
            gold, "_provider_attempt_total", return_value=0
        ):
            with self.assertRaisesRegex(RuntimeError, "Viewer Quality Contract V1 blocked release"):
                gold.run_gold_enforce_phase4(
                    output_dir=root,
                    gemini="g",
                    pexels="p",
                    pixabay=None,
                    ledger=object(),
                )

        self.assertEqual(order, ["critic", "viewer"])
        seal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
