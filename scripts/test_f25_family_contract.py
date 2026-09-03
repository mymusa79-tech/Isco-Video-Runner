from __future__ import annotations

import json
import unittest
from pathlib import Path

import scripts.production_stage_ladder as ladder
from scripts.delivery_acceptance_v2 import CONTRACT_ID
from scripts.unified_delivery import finalize_release_manifest


ROOT = Path(__file__).resolve().parents[1]
REGISTER = Path(__file__).with_name("production_family_closure.json")
WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"


class F25DeliveryTerminalAuthorityFamilyTests(unittest.TestCase):
    def test_family_registry_declares_p6_terminal_authority(self) -> None:
        registry = json.loads(REGISTER.read_text(encoding="utf-8"))
        family = next(item for item in registry["families"] if item["id"] == "F25")
        self.assertEqual(family["name"], "delivery_terminal_authority_receipt_and_remote_identity")
        self.assertEqual(family["required_phases"], ["P6"])
        self.assertIn("scripts.test_delivery_acceptance_v2", family["contracts"])
        self.assertIn("scripts.test_release_transaction", family["contracts"])
        self.assertIn("scripts.test_release_terminal_provenance_closure", family["contracts"])
        self.assertIn("scripts.test_f25_family_contract", family["contracts"])

    def test_p6_stage_ladder_executes_terminal_delivery_family(self) -> None:
        p6 = ladder.PHASE_TESTS["P6"]
        for contract in (
            "scripts.test_delivery_acceptance_v2",
            "scripts.test_release_transaction",
            "scripts.test_release_reconciliation_contracts",
            "scripts.test_release_reconciliation_journal",
            "scripts.test_release_terminal_provenance_closure",
            "scripts.test_telegram_publish_gate",
            "scripts.test_telegram_release_approval",
            "scripts.test_telegram_release_identity",
            "scripts.test_telegram_final_notify",
            "scripts.test_youtube_manual_publish_only",
            "scripts.test_f25_family_contract",
        ):
            self.assertIn(contract, p6)

    def test_legacy_finalize_seam_cannot_claim_released_state(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "delivery-manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_state": "staged",
                        "release_tag": None,
                        "delivery_url": None,
                        "youtube_publish_mode": "manual_in_youtube_studio",
                        "publication_performed": False,
                    }
                ),
                encoding="utf-8",
            )
            finalize_release_manifest(path, repository="o/r", release_tag="video-1")
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["release_state"], "staged")
            self.assertIsNone(value["release_tag"])
            self.assertIsNone(value["delivery_url"])
            self.assertEqual(value["release_candidate_tag"], "video-1")

    def test_production_workflow_seals_terminal_acceptance_after_release_transaction(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        release_marker = '"${release_cmd[@]}"'
        acceptance_marker = "python scripts/delivery_acceptance_v2.py"
        self.assertIn(release_marker, text)
        self.assertIn(acceptance_marker, text)
        self.assertLess(text.index(release_marker), text.index(acceptance_marker))
        self.assertIn("--release-receipt \"$RUNNER_TEMP/release-receipt.json\"", text)
        self.assertIn("--release-journal \"$RUNNER_TEMP/release-transaction.json\"", text)
        self.assertIn("--target-sha \"$GITHUB_SHA\"", text)
        self.assertIn("delivery-terminal-receipt.json", text)

    def test_delivery_contract_identity_is_explicit_v2(self) -> None:
        self.assertEqual(CONTRACT_ID, "delivery.acceptance.v2")


if __name__ == "__main__":
    unittest.main()
