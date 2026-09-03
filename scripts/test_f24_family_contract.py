from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class F24FamilyContractTests(unittest.TestCase):
    def test_every_f24_contract_is_declared_in_stage_ladder(self) -> None:
        register = json.loads((ROOT / "scripts" / "production_family_closure.json").read_text(encoding="utf-8"))
        f24 = next(item for item in register["families"] if item["id"] == "F24")
        tree = ast.parse((ROOT / "scripts" / "production_stage_ladder.py").read_text(encoding="utf-8"))
        phase_tests = None
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PHASE_TESTS":
                phase_tests = ast.literal_eval(node.value)
                break
        self.assertIsInstance(phase_tests, dict)
        executed = {name for phase in f24["required_phases"] for name in phase_tests[phase]}
        self.assertTrue(set(f24["contracts"]).issubset(executed))


if __name__ == "__main__":
    unittest.main()
