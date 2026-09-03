from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _git_blob_sha(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


class FinalMasterAcceptanceRegistrationTests(unittest.TestCase):
    def test_f24_is_registered_without_replacing_historical_families(self) -> None:
        register = json.loads((ROOT / "scripts" / "production_family_closure.json").read_text(encoding="utf-8"))
        families = {item["id"]: item for item in register["families"]}
        self.assertIn("F22", families)
        self.assertIn("F23", families)
        self.assertIn("F24", families)
        f24 = families["F24"]
        self.assertEqual(f24["required_phases"], ["P4", "P5", "P6"])
        self.assertIn("scripts.test_final_master_acceptance_v2", f24["contracts"])
        self.assertIn("scripts.test_gold_enforce_phase4", f24["contracts"])
        self.assertIn("scripts.test_unified_delivery", f24["contracts"])
        self.assertIn("scripts.test_unified_delivery_canonical", f24["contracts"])

    def test_certified_qc_core_and_stable_port_remain_byte_identical(self) -> None:
        self.assertEqual(
            _git_blob_sha(ROOT / "scripts" / "final_master_qc.py"),
            "e3412fc5710618eb9d7529710d8dbbc539e9fa91",
        )
        self.assertEqual(
            _git_blob_sha(ROOT / "scripts" / "orchestration_qc_port.py"),
            "9d23051dc3db8ad8f5913dd5a21dcc2f4bee7035",
        )

    def test_runtime_wrapper_seals_f24_above_certified_qc_port(self) -> None:
        port = (ROOT / "scripts" / "orchestration_qc_port.py").read_text(encoding="utf-8")
        durability = (ROOT / "scripts" / "final_qc_observer_durability.py").read_text(encoding="utf-8")
        self.assertNotIn("final_master_acceptance_v2", port)
        self.assertIn("seal_final_master_acceptance", durability)
        self.assertIn("require_final_master_acceptance", durability)
        self.assertIn("_install_final_qc_durability()", durability)
        self.assertIn("_isco_final_master_acceptance_v2", durability)

    def test_gold_and_delivery_revalidate_p4_identity(self) -> None:
        gold = (ROOT / "scripts" / "gold_enforce_phase4.py").read_text(encoding="utf-8")
        delivery = (ROOT / "scripts" / "unified_delivery.py").read_text(encoding="utf-8")
        self.assertIn("require_final_master_acceptance(output_dir)", gold)
        self.assertIn("require_final_master_acceptance(root)", delivery)
        self.assertIn("require_certified_final_video", delivery)

    def test_planning_and_sibling_orchestration_do_not_import_f24(self) -> None:
        planning = (ROOT / "scripts" / "planning_runtime_contract.py").read_text(encoding="utf-8")
        sibling = (ROOT / "scripts" / "sibling_short_orchestration.py").read_text(encoding="utf-8")
        self.assertNotIn("final_master_acceptance_v2", planning)
        self.assertNotIn("final_master_acceptance_v2", sibling)


if __name__ == "__main__":
    unittest.main()
