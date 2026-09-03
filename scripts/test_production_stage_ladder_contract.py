from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts.production_stage_ladder import BASELINE_SHA256, BASELINE_SIZE, PHASES, PHASE_TESTS

ROOT = Path(__file__).resolve().parents[1]
REGISTER = ROOT / "scripts" / "production_family_closure.json"
LADDER_WORKFLOW = ROOT / ".github" / "workflows" / "verify-production-stage-ladder.yml"
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
ENVIRONMENT_PREFLIGHT = ROOT / "scripts" / "environment_preflight.py"
CHECKPOINT_WRAPPER = ROOT / "scripts" / "planning_checkpoint_state.py"
CHECKPOINT_CORE = ROOT / "scripts" / "planning_checkpoint_state_core.py"
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

    def test_register_exactly_covers_every_run_51_through_170(self) -> None:
        data = json.loads(REGISTER.read_text(encoding="utf-8"))
        window = data["historical_window"]
        self.assertEqual((window["first_run"], window["last_run"]), (51, 170))
        expected = set(range(51, 171))
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
        executed = {module for modules in PHASE_TESTS.values() for module in modules}
        ids: set[str] = set()
        for family in data["families"]:
            self.assertNotIn(family["id"], ids)
            ids.add(family["id"])
            self.assertTrue(family["required_phases"])
            self.assertTrue(set(family["required_phases"]).issubset(set(PHASES)))
            self.assertTrue(family["contracts"])
            for module in family["contracts"]:
                self.assertTrue(_module_path(module).is_file(), f"{family['id']} missing {module}")
                self.assertIn(module, executed, f"{family['id']} contract not executed by Stage Ladder: {module}")

    def test_ladder_replays_video50_in_phase_order_and_never_dispatches_production(self) -> None:
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

    def test_runtime_temp_paths_are_initialized_inside_a_step(self) -> None:
        text = LADDER_WORKFLOW.read_text(encoding="utf-8")
        job_header = text.split("    steps:", 1)[0]
        self.assertNotIn("${{ runner.temp }}", job_header)
        self.assertIn("Initialize Stage Ladder runtime paths", text)
        self.assertIn('root="$RUNNER_TEMP/production-stage-ladder"', text)
        self.assertIn('echo "EVIDENCE_DIR=$root/evidence"', text)
        self.assertIn('>> "$GITHUB_ENV"', text)

    def test_only_green_main_push_can_publish_exact_sha_certification_ref(self) -> None:
        text = LADDER_WORKFLOW.read_text(encoding="utf-8")
        marker = "Publish exact-SHA Stage Ladder certification ref"
        self.assertIn(marker, text)
        tail = text[text.index(marker):]
        self.assertIn("github.event_name == 'push'", tail)
        self.assertIn("github.ref == 'refs/heads/main'", tail)
        self.assertIn('tag="stage-ladder-green-$CANDIDATE_SHA"', tail)
        self.assertIn('-f sha="$CANDIDATE_SHA"', tail)

    def test_production_preflight_requires_exact_sha_ladder_before_provider_secrets(self) -> None:
        production = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(
            production.index("Verify production environment and release namespace"),
            production.index("Materialize approved production secrets"),
        )
        preflight = ENVIRONMENT_PREFLIGHT.read_text(encoding="utf-8")
        self.assertIn("runtime_phase import activate_canonical_runtime, canonical_workflow_identity", preflight)
        self.assertIn("if canonical_workflow_identity():", preflight)
        self.assertIn("activate_canonical_runtime(persist_workflow_env=False)", preflight)
        self.assertIn("require_exact_sha_stage_ladder(", preflight)
        self.assertIn('sha=os.environ.get("GITHUB_SHA")', preflight)
        self.assertLess(
            preflight.index("require_exact_sha_stage_ladder("),
            preflight.index("materialize_runtime_github_token(token)"),
        )

    def test_legacy_checkpoint_runtime_identity_is_inert_behind_explicit_wrapper(self) -> None:
        wrapper = CHECKPOINT_WRAPPER.read_text(encoding="utf-8")
        core = CHECKPOINT_CORE.read_text(encoding="utf-8")
        self.assertIn("runtime_phase import canonical_runtime_enabled as _canonical_runtime_enabled", wrapper)
        self.assertIn("_core.canonical_runtime_enabled = _canonical_runtime_enabled", wrapper)
        self.assertIn("def canonical_runtime_enabled()", core)
        self.assertNotIn("GITHUB_WORKFLOW_REF", wrapper)
        self.assertNotIn("GITHUB_EVENT_NAME", wrapper)

    def test_provider_capacity_policy_has_no_stale_fixed_groq_tpm_truth(self) -> None:
        data = json.loads(RELIABILITY_MATRIX.read_text(encoding="utf-8"))
        planning = data["planning_provider_reliability"]
        self.assertNotIn("groq_free_tpm_limit", planning)
        self.assertEqual(planning["groq_capacity_source"], "runtime_provider_evidence_model_scoped")


if __name__ == "__main__":
    unittest.main()
