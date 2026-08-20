from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.packaging_delivery_contract import EXPECTED_THUMBNAILS, validate_packaging_delivery


class PackagingArtifactDeliveryP0ETests(unittest.TestCase):
    def _fixture(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        candidates = []
        rights = []
        for index, filename in enumerate(EXPECTED_THUMBNAILS, 1):
            (root / filename).write_bytes(b"jpeg-fixture" * 200)
            provider = "pixabay" if index == 3 else "pexels"
            candidates.append(
                {
                    "candidate_id": f"packaging-{chr(96 + index)}",
                    "file": filename,
                    "photo_provider": provider,
                }
            )
            rights.append(
                {
                    "provider": provider,
                    "provider_asset_id": 1000 + index,
                    "output_file": filename,
                    "license_url": (
                        "https://pixabay.com/service/license-summary/"
                        if provider == "pixabay"
                        else "https://www.pexels.com/license/"
                    ),
                }
            )
        (root / "thumbnail-plan.json").write_text(
            json.dumps({"status": "ready", "candidates": candidates}), encoding="utf-8"
        )
        (root / "rights-manifest.json").write_text(
            json.dumps({"thumbnails": rights}), encoding="utf-8"
        )
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "phase": "4",
                    "mode": "enforce",
                    "release_authority": "gold",
                    "single_render": True,
                    "gold": {"accepted": True},
                    "same_render": {"artifact_divergence": False},
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_valid_mixed_provider_package_is_deliverable(self) -> None:
        root = self._fixture()
        result = validate_packaging_delivery(root)
        self.assertEqual(result["thumbnail_1"].name, "thumbnail-1.jpg")
        self.assertEqual(result["thumbnail_3"].name, "thumbnail-3.jpg")
        self.assertEqual(result["rights_manifest"].name, "rights-manifest.json")

    def test_missing_thumbnail_fails_closed(self) -> None:
        root = self._fixture()
        (root / "thumbnail-2.jpg").unlink()
        with self.assertRaisesRegex(RuntimeError, "Missing reviewed thumbnail"):
            validate_packaging_delivery(root)

    def test_rights_binding_mismatch_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "rights-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["thumbnails"][1]["output_file"] = "wrong.jpg"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "rights records do not bind"):
            validate_packaging_delivery(root)

    def test_missing_provider_provenance_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "rights-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["thumbnails"][2]["provider_asset_id"] = None
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Missing provider asset ID"):
            validate_packaging_delivery(root)

    def test_gold_divergence_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "gold-enforce-report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["same_render"]["artifact_divergence"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "render diverged"):
            validate_packaging_delivery(root)

    def test_workflow_delivers_packaging_without_expanding_production_trigger(self) -> None:
        workflow = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        trigger_contract = workflow.split("concurrency:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger_contract)
        self.assertNotIn("push:", trigger_contract)
        self.assertNotIn("inputs:", trigger_contract)
        self.assertNotIn('".github/workflows/produce-resilient-v4.yml"', trigger_contract)
        self.assertIn("engine/output/*/thumbnail-plan.json", workflow)
        self.assertIn("${{ steps.final_review.outputs.output_root }}/thumbnail-*.jpg", workflow)
        self.assertIn("packaging_delivery_contract import validate_packaging_delivery", workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/thumbnail-plan.json"', workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/thumbnail-3.jpg"', workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/rights-manifest.json"', workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/gold-enforce-report.json"', workflow)


if __name__ == "__main__":
    unittest.main()
