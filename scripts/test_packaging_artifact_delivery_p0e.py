from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.packaging_delivery_contract as packaging
from scripts.packaging_delivery_contract import (
    ACCEPTANCE_FILENAME,
    CONTRACT_ID,
    EXPECTED_THUMBNAILS,
    gold_packaging_acceptance_sha256,
    seal_gold_packaging_acceptance,
    validate_packaging_delivery,
)


class PackagingArtifactDeliveryP0ETests(unittest.TestCase):
    def _common_files(self, root: Path, *, fmt: str) -> None:
        (root / "final.mp4").write_bytes(b"final-video-fixture" * 100)
        (root / "plan.json").write_text(json.dumps({"format": fmt}), encoding="utf-8")
        (root / "final-critic.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")

    def _write_gold_report(self, root: Path, profile: str) -> None:
        embedded = json.loads((root / ACCEPTANCE_FILENAME).read_text(encoding="utf-8"))
        (root / "gold-enforce-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "phase": "4",
                    "mode": "enforce",
                    "release_authority": "gold",
                    "single_render": True,
                    "gold": {"accepted": True},
                    "same_render": {"artifact_divergence": False},
                    "packaging_acceptance": {
                        "required": True,
                        "present": True,
                        "contract_id": CONTRACT_ID,
                        "profile": profile,
                        "certificate_file": ACCEPTANCE_FILENAME,
                        "certificate_sha256": gold_packaging_acceptance_sha256(root),
                        "embedded_certificate": embedded,
                        "sealed_before_state_acceptance": True,
                    },
                }
            ),
            encoding="utf-8",
        )

    def _fixture(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self._common_files(root, fmt="film")
        candidates = []
        rights = []
        for index, filename in enumerate(EXPECTED_THUMBNAILS, 1):
            (root / filename).write_bytes(b"jpeg-fixture" * 200)
            provider = "pixabay" if index == 3 else "pexels"
            asset_id = 1000 + index
            candidates.append(
                {
                    "candidate_id": f"packaging-{chr(96 + index)}",
                    "experiment_slot": chr(64 + index),
                    "file": filename,
                    "photo_provider": provider,
                    "photo_id": asset_id,
                }
            )
            rights.append(
                {
                    "provider": provider,
                    "provider_asset_id": asset_id,
                    "output_file": filename,
                    "license_url": (
                        "https://pixabay.com/service/license-summary/"
                        if provider == "pixabay"
                        else "https://www.pexels.com/license/"
                    ),
                }
            )
        (root / "thumbnail-plan.json").write_text(
            json.dumps(
                {
                    "status": "ready",
                    "package_type": "title_thumbnail_hypothesis_set",
                    "candidates": candidates,
                }
            ),
            encoding="utf-8",
        )
        (root / "rights-manifest.json").write_text(
            json.dumps({"thumbnails": rights}), encoding="utf-8"
        )
        acceptance = seal_gold_packaging_acceptance(root, critic={"status": "pass"})
        self._write_gold_report(root, acceptance["profile"])
        return root

    def _short_fixture(self) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self._common_files(root, fmt="moment")
        (root / "thumbnail-plan.json").write_text(
            json.dumps(
                {
                    "status": "not_applicable_to_shorts",
                    "packaging_contract_version": 2,
                    "reason": "Select a truthful frame during manual Shorts upload.",
                    "candidates": [],
                }
            ),
            encoding="utf-8",
        )
        (root / "rights-manifest.json").write_text(
            json.dumps({"visuals": [{"provider": "pexels", "provider_asset_id": 55}], "thumbnails": []}),
            encoding="utf-8",
        )

        def fake_extract(_final: Path, output: Path, timestamp: float) -> None:
            output.write_bytes((f"frame@{timestamp:.3f}".encode("utf-8") + b"-") * 180)

        with patch.object(packaging.media_ffmpeg, "duration", return_value=12.0), patch.object(
            packaging, "_extract_frame", side_effect=fake_extract
        ) as extract:
            acceptance = seal_gold_packaging_acceptance(root, critic={"status": "pass"})
        self.assertEqual(extract.call_count, 3)
        self._write_gold_report(root, acceptance["profile"])
        return root

    def test_valid_mixed_provider_package_is_deliverable(self) -> None:
        root = self._fixture()
        result = validate_packaging_delivery(root)
        self.assertEqual(result["thumbnail_1"].name, "thumbnail-1.jpg")
        self.assertEqual(result["thumbnail_3"].name, "thumbnail-3.jpg")
        self.assertEqual(result["rights_manifest"].name, "rights-manifest.json")
        self.assertEqual(result["gold_packaging_acceptance"].name, ACCEPTANCE_FILENAME)

    def test_standalone_short_is_deliverable_with_zero_custom_candidates_and_three_same_render_selection_aids(self) -> None:
        root = self._short_fixture()
        result = validate_packaging_delivery(root)
        package = json.loads((root / "thumbnail-plan.json").read_text(encoding="utf-8"))
        rights = json.loads((root / "rights-manifest.json").read_text(encoding="utf-8"))
        receipt = json.loads((root / ACCEPTANCE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(package["candidates"], [])
        self.assertEqual(rights["thumbnails"], [])
        self.assertEqual(len(receipt["short_frame_selection"]), 3)
        self.assertEqual(receipt["profile"], "short_truthful_frame_selection")
        self.assertTrue(all(item["source_file"] == "final.mp4" for item in receipt["short_frame_selection"]))
        self.assertEqual(result["thumbnail_2"].name, "thumbnail-2.jpg")

    def test_exact_thumbnail_tamper_after_gold_seal_fails_closed(self) -> None:
        root = self._fixture()
        (root / "thumbnail-2.jpg").write_bytes(b"tampered" * 400)
        with self.assertRaisesRegex(RuntimeError, "exact-artifact identity mismatch"):
            validate_packaging_delivery(root)

    def test_exact_title_plan_tamper_after_gold_seal_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "thumbnail-plan.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["candidates"][0]["candidate_id"] = "tampered-a"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exact-artifact identity mismatch"):
            validate_packaging_delivery(root)

    def test_exact_rights_tamper_after_gold_seal_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "rights-manifest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["thumbnails"][2]["provider_asset_id"] = 999999
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "exact-artifact identity mismatch"):
            validate_packaging_delivery(root)

    def test_budget_fallback_final_render_derivatives_are_deliverable_with_inherited_rights(self) -> None:
        root = self._fixture()
        package_path = root / "thumbnail-plan.json"
        rights_path = root / "rights-manifest.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["budget_degraded"] = True
        package["budget_fallback"] = {
            "reason": "p2_provider_attempt_capacity_exhausted",
            "provider_attempts_consumed": 0,
        }
        rights = json.loads(rights_path.read_text(encoding="utf-8"))
        rights["thumbnail_rights_mode"] = "derived_from_already_rights_cleared_final_render"
        rights["visuals"] = [{"provider": "pexels", "provider_asset_id": 55}]
        for index, item in enumerate(package["candidates"], 1):
            item["photo_provider"] = "derived_final_render"
            item["photo_id"] = f"final.mp4@{index * 10:.3f}s"
        rights["thumbnails"] = [
            {
                "provider": "derived_final_render",
                "provider_asset_id": f"final.mp4@{index * 10:.3f}s",
                "output_file": filename,
                "license_url": None,
                "source_file": "final.mp4",
                "source_timestamp_seconds": float(index * 10),
                "rights_inheritance": "rights-manifest.visuals",
                "inherited_visual_rights_count": 1,
            }
            for index, filename in enumerate(EXPECTED_THUMBNAILS, 1)
        ]
        package_path.write_text(json.dumps(package), encoding="utf-8")
        rights_path.write_text(json.dumps(rights), encoding="utf-8")
        acceptance = seal_gold_packaging_acceptance(root, critic={"status": "pass"})
        self._write_gold_report(root, acceptance["profile"])

        result = validate_packaging_delivery(root)
        self.assertEqual(result["thumbnail_2"].name, "thumbnail-2.jpg")

    def test_derived_thumbnail_without_budget_degradation_fails_closed(self) -> None:
        root = self._fixture()
        rights_path = root / "rights-manifest.json"
        rights = json.loads(rights_path.read_text(encoding="utf-8"))
        rights["thumbnails"][0].update(
            {
                "provider": "derived_final_render",
                "provider_asset_id": "final.mp4@10.000s",
                "source_file": "final.mp4",
                "rights_inheritance": "rights-manifest.visuals",
                "inherited_visual_rights_count": 1,
            }
        )
        rights_path.write_text(json.dumps(rights), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            validate_packaging_delivery(root)

    def test_missing_thumbnail_fails_closed(self) -> None:
        root = self._fixture()
        (root / "thumbnail-2.jpg").unlink()
        with self.assertRaises(RuntimeError):
            validate_packaging_delivery(root)

    def test_gold_divergence_fails_closed(self) -> None:
        root = self._fixture()
        path = root / "gold-enforce-report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["same_render"]["artifact_divergence"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "render diverged"):
            validate_packaging_delivery(root)

    def test_gold_report_must_bind_exact_acceptance_certificate_sha(self) -> None:
        root = self._fixture()
        path = root / "gold-enforce-report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["packaging_acceptance"]["certificate_sha256"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "acceptance SHA mismatch"):
            validate_packaging_delivery(root)

    def test_gold_report_embedded_certificate_must_match_local_receipt(self) -> None:
        root = self._fixture()
        path = root / "gold-enforce-report.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["packaging_acceptance"]["embedded_certificate"]["decision"] = "block"
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "embedded packaging certificate mismatch"):
            validate_packaging_delivery(root)

    def test_workflow_delivers_f25_evidence_without_expanding_production_trigger(self) -> None:
        workflow = Path(".github/workflows/produce-resilient-v4.yml").read_text(encoding="utf-8")
        trigger_contract = workflow.split("concurrency:", 1)[0]
        self.assertIn("workflow_dispatch:", trigger_contract)
        for automatic_trigger in ("push:", "pull_request:", "schedule:", "repository_dispatch:", "workflow_call:"):
            self.assertNotIn(automatic_trigger, trigger_contract)

        dispatch_inputs = set(
            re.findall(r"^      ([A-Za-z0-9_]+):\s*$", trigger_contract, flags=re.MULTILINE)
        )
        self.assertEqual(
            dispatch_inputs,
            {"request_id", "request_sha256", "authorization_id", "engine_sha"},
        )
        self.assertEqual(trigger_contract.count("required: false"), 4)
        self.assertEqual(trigger_contract.count('default: ""'), 4)
        self.assertEqual(trigger_contract.count("type: string"), 4)
        self.assertNotIn('".github/workflows/produce-resilient-v4.yml"', trigger_contract)
        self.assertIn("engine/output/*/thumbnail-plan.json", workflow)
        self.assertIn("${{ steps.final_review.outputs.output_root }}/thumbnail-*.jpg", workflow)
        self.assertIn("packaging_delivery_contract import validate_packaging_delivery", workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/thumbnail-plan.json"', workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/thumbnail-3.jpg"', workflow)
        self.assertIn('"$FINAL_OUTPUT_ROOT/rights-manifest.json"', workflow)
        # gold-enforce-report.json is an explicit GitHub Release asset and embeds the
        # complete F25 certificate plus its sidecar SHA for durable audit evidence.
        self.assertIn('"$FINAL_OUTPUT_ROOT/gold-enforce-report.json"', workflow)
        self.assertEqual(ACCEPTANCE_FILENAME, "gold-packaging-acceptance.json")


if __name__ == "__main__":
    unittest.main()
