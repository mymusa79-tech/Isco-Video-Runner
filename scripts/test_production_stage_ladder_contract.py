from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.production_stage_ladder import (
    BASELINE_SHA256,
    BASELINE_SIZE,
    PHASES,
    PHASE_TESTS,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts" / "production_family_closure.json"
LADDER_WORKFLOW = ROOT / ".github" / "workflows" / "verify-production-stage-ladder.yml"
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
RELIABILITY_MATRIX = ROOT / "scripts" / "reliability_failure_matrix.json"


def _module_path(module: str) -> Path:
    return ROOT / (module.replace(".", "/") + ".py")


def _expand(spec: str) -> set[int]:
    spec = str(spec).strip()
    if "-" not in spec:
        return {int(spec)}
    left, right = spec.split("-", 1)
    return set(range(int(left), int(right) + 1))


class ProductionStageLadderContractTests(unittest.TestCase):
    def test_phase_set_is_exactly_p0_through_p6_and_all_modules_exist(self) -> None:
        self.assertEqual(PHASES, ("P0", "P1", "P2", "P3", "P4", "P5", "P6"))
        self.assertEqual(set(PHASE_TESTS), set(PHASES))
        for phase, modules in PHASE_TESTS.items():
            self.assertTrue(modules, phase)
            for module in modules:
                self.assertTrue(_module_path(module).is_file(), f"{phase} missing {module}")

    def test_register_exactly_covers_every_run_51_through_130(self) -> None:
        data = json.loads(REGISTER.read_text(encoding="utf-8"))
        window = data["historical_window"]
        self.assertEqual((window["first_run"], window["last_run"]), (51, 130))
        expected = set(range(51, 131))
        seen: set[int] = set()
        for cohort in data["audit_cohorts"]:
            runs = _expand(cohort["runs"])
            self.assertFalse(seen & runs, f"overlapping audit cohort {cohort['id']}")
            seen.update(runs)
            self.assertTrue(set(cohort["required_phases"]).issubset(set(PHASES)))
        self.assertEqual(seen, expected)

    def test_register_is_bound_to_real_video50_and_live_contracts(self) -> None:
        data = json.loads(REGISTER.read_text(encoding="utf-8"))
        baseline = data["historical_window"]["known_good_baseline"]
        self.assertEqual(baseline["release_tag"], "video-50")
        self.assertEqual(baseline["asset"], "final.mp4")
        self.assertEqual(baseline["size_bytes"], BASELINE_SIZE)
        self.assertEqual(baseline["sha256"], BASELINE_SHA256)
        self.assertFalse(data["closure_policy"]["historical_fix_alone_is_closure_evidence"])
        self.assertFalse(data["closure_policy"]["production_dispatch_allowed_by_this_register"])
        ids: set[str] = set()
        for family in data["families"]:
            self.assertNotIn(family["id"], ids)
            ids.add(family["id"])
            self.assertTrue(family["required_phases"])
            self.assertTrue(set(family["required_phases"]).issubset(set(PHASES)))
            self.assertTrue(family["contracts"])
            for module in family["contracts"]:
                self.assertTrue(_module_path(module).is_file(), f"{family['id']} missing {module}")

    def test_ladder_workflow_replays_real_baseline_sequentially_without_dispatch(self) -> None:
        text = LADDER_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("push:\n    branches: [\"main\"]", text)
        self.assertIn("pull_request:\n    branches: [\"main\"]", text)
        self.assertIn("gh release download video-50", text)
        self.assertIn(str(BASELINE_SIZE), text)
        self.assertIn(BASELINE_SHA256, text)
        positions = [text.index(f"phase {phase}") for phase in PHASES]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("stage-ladder-certification.json", text)
        self.assertNotIn("gh workflow run produce-resilient-v4", text)
        self.assertNotIn("workflow_call:", text)
        self.assertNotIn("repository_dispatch", text)

    def test_production_requires_same_sha_ladder_before_provider_secrets(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("actions: read", text)
        marker = "Require exact-SHA Production Stage Ladder certification"
        self.assertIn(marker, text)
        gate = text.index(marker)
        provider = text.index("Materialize approved production secrets")
        self.assertLess(gate, provider)
        self.assertIn("verify-production-stage-ladder.yml", text[gate:provider])
        self.assertIn("GITHUB_SHA", text[gate:provider])
        self.assertIn("conclusion", text[gate:provider])
        self.assertIn("success", text[gate:provider])

    def test_provider_capacity_policy_has_no_stale_fixed_groq_tpm_truth(self) -> None:
        data = json.loads(RELIABILITY_MATRIX.read_text(encoding="utf-8"))
        planning = data["planning_provider_reliability"]
        self.assertNotIn("groq_free_tpm_limit", planning)
        self.assertEqual(planning["groq_capacity_source"], "runtime_provider_evidence_model_scoped")


if __name__ == "__main__":
    unittest.main()
