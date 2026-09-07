from __future__ import annotations

import inspect
import unittest

from scripts import planning_runtime_contract


class StructuralEditorialInstallationTests(unittest.TestCase):
    def test_canonical_planning_seam_installs_structural_contract_after_quality_guard(self) -> None:
        source = inspect.getsource(planning_runtime_contract.install_entrypoint_planning_contracts)
        quality = source.index("install_planner_quality_guard()")
        structural = source.index("install_structural_editorial_contract()")
        stage_boundaries = source.index("install_planning_stage_boundaries()")
        self.assertLess(quality, structural)
        self.assertLess(structural, stage_boundaries)

    def test_structural_contract_is_imported_by_canonical_planning_root(self) -> None:
        source = inspect.getsource(planning_runtime_contract)
        self.assertIn(
            "from scripts.structural_editorial_contract import install_structural_editorial_contract",
            source,
        )


if __name__ == "__main__":
    unittest.main()
