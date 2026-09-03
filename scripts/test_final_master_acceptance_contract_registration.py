from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FinalMasterAcceptanceRegistrationTests(unittest.TestCase):
    def test_f24_is_registered_without_replacing_historical_families(self) -> None:
        register = json.loads((ROOT / "scripts" / "production_family_closure.json").read_text(encoding="utf-8"))
        families = {item["id"]: item for item in register["families"]}
        self.assertIn("F22", families)
        self.assertIn("F23", families)
        self.assertIn("F24", families)
        f24 = families["F24"]
        self.assertEqual(f24["required_phases"], ["P4", "P5", "P6"])
        self.assertIn("scripts.test_final_master_qc", f24["contracts"])
        self.assertIn("scripts.test_gold_enforce_phase4", f24["contracts"])
        self.assertIn("scripts.test_unified_delivery", f24["contracts"])
        self.assertIn("scripts.test_unified_delivery_canonical", f24["contracts"])

    def test_qc_port_requires_exact_receipt_before_return(self) -> None:
        source = (ROOT / "scripts" / "orchestration_qc_port.py").read_text(encoding="utf-8")
        self.assertIn("require_final_master_acceptance", source)
        self.assertLess(source.index("core.run_final_master_qc"), source.index("require_final_master_acceptance(Path(output_dir)"))

    def test_gold_and_delivery_revalidate_p4_identity(self) -> None:
        gold = (ROOT / "scripts" / "gold_enforce_phase4.py").read_text(encoding="utf-8")
        delivery = (ROOT / "scripts" / "unified_delivery.py").read_text(encoding="utf-8")
        self.assertIn("require_final_master_acceptance(output_dir)", gold)
        self.assertIn("require_final_master_acceptance(root)", delivery)
        self.assertIn("require_certified_final_video", delivery)


if __name__ == "__main__":
    unittest.main()
