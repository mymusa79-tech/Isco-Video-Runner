from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "verify-engine-candidate.yml"


class EngineCandidateVerifierContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text(encoding="utf-8")

    def test_permanent_candidate_verifier_accepts_exact_engine_sha(self) -> None:
        self.assertIn("name: Verify Engine Candidate", self.text)
        self.assertIn("workflow_dispatch:", self.text)
        self.assertRegex(self.text, r"engine_sha:\s*\n\s+description:")
        self.assertIn("required: true", self.text)
        self.assertIn("Engine candidate must be an exact lowercase 40-character SHA", self.text)
        self.assertIn("repository: mymusa79-tech/Isco-Video-Agent", self.text)
        self.assertIn("ref: ${{ steps.engine.outputs.sha }}", self.text)

    def test_verifier_runs_full_engine_runner_and_approved_brief_matrix(self) -> None:
        for required in (
            "Full Engine suite",
            "Approved-Brief CLI contract matrix",
            "Full Runner regression against candidate Engine",
            "Dependency audit",
            "Certify Engine source hermeticity after Full Runner regression",
        ):
            self.assertIn(required, self.text)

    def test_verifier_is_not_production_authority(self) -> None:
        self.assertIn("permissions:\n  contents: read", self.text)
        self.assertNotIn("contents: write", self.text)
        self.assertNotIn("full-regression-green-", self.text)
        self.assertNotIn("stage-ladder-green-", self.text)
        self.assertNotRegex(self.text, r"gh\s+workflow\s+run\s+.*produce-resilient-v4")
        self.assertNotRegex(self.text, r"actions/workflows/produce-resilient-v4\.yml/dispatches")
        self.assertIn('"production_authority": False', self.text)
        self.assertIn('"certification_ref_published": False', self.text)
        self.assertIn('"production_dispatch_performed": False', self.text)

    def test_pull_request_mode_self_tests_only_against_canonical_pin(self) -> None:
        self.assertIn('source = "canonical_pr_self_test"', self.text)
        self.assertIn('.github/workflows/produce-resilient-v4.yml', self.text)
        self.assertIn("Canonical V4 Engine pin mismatch", self.text)
        self.assertNotIn("push:\n    branches: [\"main\"]", self.text)


if __name__ == "__main__":
    unittest.main()
