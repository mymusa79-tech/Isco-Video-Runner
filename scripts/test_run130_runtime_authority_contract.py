from __future__ import annotations

import unittest
from pathlib import Path

from scripts.planning_checkpoint_state import planning_contract_files


ROOT = Path(__file__).resolve().parents[1]


class Run130RuntimeAuthorityContractTests(unittest.TestCase):
    def test_live_closure_has_one_ambient_ci_runtime_authority(self) -> None:
        """Only runtime_phase may interpret GitHub workflow context as phase identity.

        planning_checkpoint_state retains its historical helper for internal/backward
        compatibility, but it is not allowed to become a live caller-owned authority.
        Every other production-reachable module must use runtime_phase instead of
        inspecting GITHUB_WORKFLOW_REF/GITHUB_EVENT_NAME to decide that production is
        active.
        """
        closure = set(planning_contract_files(ROOT))
        allowed_ambient = {
            "scripts/runtime_phase.py",
            "scripts/planning_checkpoint_state.py",  # legacy internal secondary gate only
        }
        violations: list[str] = []
        for relative in sorted(closure):
            if relative in allowed_ambient:
                continue
            text = (ROOT / relative).read_text(encoding="utf-8")
            if "GITHUB_WORKFLOW_REF" in text or "GITHUB_EVENT_NAME" in text:
                violations.append(relative)
        self.assertEqual(violations, [], f"live modules infer runtime from ambient CI: {violations}")

    def test_no_live_module_calls_legacy_checkpoint_runtime_authority(self) -> None:
        closure = set(planning_contract_files(ROOT))
        violations: list[str] = []
        needles = (
            "checkpoint.canonical_runtime_enabled(",
            "planning_checkpoint_state.canonical_runtime_enabled(",
            "from scripts.planning_checkpoint_state import canonical_runtime_enabled",
            "from planning_checkpoint_state import canonical_runtime_enabled",
        )
        for relative in sorted(closure):
            if relative == "scripts/planning_checkpoint_state.py":
                continue
            text = (ROOT / relative).read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                violations.append(relative)
        self.assertEqual(violations, [], f"live modules use legacy runtime authority: {violations}")

    def test_runtime_phase_requires_explicit_application_activation(self) -> None:
        text = (ROOT / "scripts/runtime_phase.py").read_text(encoding="utf-8")
        self.assertIn('"ISCO_CANONICAL_RUNTIME"', text)
        self.assertIn("canonical_workflow_identity() and explicit in _TRUE_VALUES", text)

    def test_snapshot_and_runtime_closure_import_explicit_authority(self) -> None:
        for relative in (
            "scripts/immutable_planning_snapshot.py",
            "scripts/runtime_closure.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("runtime_phase import canonical_runtime_enabled", text, relative)


if __name__ == "__main__":
    unittest.main()
