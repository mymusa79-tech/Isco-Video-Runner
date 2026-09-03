from __future__ import annotations

import unittest
from pathlib import Path

from scripts.planning_checkpoint_state import planning_contract_files

ROOT = Path(__file__).resolve().parents[1]


class Run130RuntimeAuthorityContractTests(unittest.TestCase):
    def test_live_closure_has_one_active_ambient_ci_runtime_authority(self) -> None:
        """Only runtime_phase may actively interpret GitHub context as runtime phase.

        The historical checkpoint implementation is retained byte-for-byte in a
        compatibility core, but its module-global authority is replaced by the
        runtime_phase function before any exported checkpoint function can execute.
        """
        closure = set(planning_contract_files(ROOT))
        allowed_text_only = {
            "scripts/runtime_phase.py",
            "scripts/planning_checkpoint_state_core.py",
        }
        violations: list[str] = []
        for relative in sorted(closure):
            if relative in allowed_text_only:
                continue
            text = (ROOT / relative).read_text(encoding="utf-8")
            if "GITHUB_WORKFLOW_REF" in text or "GITHUB_EVENT_NAME" in text:
                violations.append(relative)
        self.assertEqual(violations, [], f"live modules infer runtime from ambient CI: {violations}")

    def test_checkpoint_wrapper_overrides_historical_authority_before_export(self) -> None:
        wrapper = (ROOT / "scripts/planning_checkpoint_state.py").read_text(encoding="utf-8")
        self.assertIn("runtime_phase import canonical_runtime_enabled as _canonical_runtime_enabled", wrapper)
        patch_i = wrapper.index("_core.canonical_runtime_enabled = _canonical_runtime_enabled")
        export_i = wrapper.index("for _name in dir(_core)")
        self.assertLess(patch_i, export_i)
        self.assertNotIn("GITHUB_WORKFLOW_REF", wrapper)
        self.assertNotIn("GITHUB_EVENT_NAME", wrapper)

    def test_no_live_module_calls_checkpoint_runtime_authority(self) -> None:
        closure = set(planning_contract_files(ROOT))
        violations: list[str] = []
        needles = (
            "checkpoint.canonical_runtime_enabled(",
            "planning_checkpoint_state.canonical_runtime_enabled(",
            "from scripts.planning_checkpoint_state import canonical_runtime_enabled",
            "from planning_checkpoint_state import canonical_runtime_enabled",
            "planning_checkpoint_state_core import canonical_runtime_enabled",
        )
        for relative in sorted(closure):
            if relative in {"scripts/planning_checkpoint_state.py", "scripts/planning_checkpoint_state_core.py"}:
                continue
            text = (ROOT / relative).read_text(encoding="utf-8")
            if any(needle in text for needle in needles):
                violations.append(relative)
        self.assertEqual(violations, [], f"live modules use checkpoint runtime authority: {violations}")

    def test_compatibility_core_is_imported_only_by_the_wrapper(self) -> None:
        closure = set(planning_contract_files(ROOT))
        users: list[str] = []
        for relative in sorted(closure):
            if relative == "scripts/planning_checkpoint_state_core.py":
                continue
            text = (ROOT / relative).read_text(encoding="utf-8")
            if "planning_checkpoint_state_core" in text:
                users.append(relative)
        self.assertEqual(users, ["scripts/planning_checkpoint_state.py"])

    def test_runtime_phase_requires_explicit_application_activation(self) -> None:
        text = (ROOT / "scripts/runtime_phase.py").read_text(encoding="utf-8")
        self.assertIn('"ISCO_CANONICAL_RUNTIME"', text)
        self.assertIn("canonical_workflow_identity() and explicit in _TRUE_VALUES", text)

    def test_snapshot_and_runtime_closure_use_live_authority(self) -> None:
        for relative in (
            "scripts/immutable_planning_snapshot.py",
            "scripts/runtime_closure.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("runtime_phase import canonical_runtime_enabled", text, relative)
            self.assertIn("canonical_runtime_enabled()", text, relative)

    def test_environment_preflight_uses_workflow_identity_without_exporting_live_phase(self) -> None:
        text = (ROOT / "scripts/environment_preflight.py").read_text(encoding="utf-8")
        self.assertIn("runtime_phase import activate_canonical_runtime, canonical_workflow_identity", text)
        self.assertIn("if not canonical_workflow_identity():", text)
        self.assertIn("activate_canonical_runtime(persist_workflow_env=False)", text)
        self.assertNotIn("if canonical_runtime_enabled():", text)


if __name__ == "__main__":
    unittest.main()
