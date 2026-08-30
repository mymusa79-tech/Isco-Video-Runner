from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
FULL_REGRESSION = WORKFLOWS / "verify-private-engine.yml"
STAGE_LADDER = WORKFLOWS / "verify-production-stage-ladder.yml"
PRODUCTION = WORKFLOWS / "produce-resilient-v4.yml"


class CIExactSHACertificationTests(unittest.TestCase):
    """Freeze T3 exact-identity certification before Production Fast Path exists."""

    def test_full_regression_certifies_exact_pr_head_or_main_sha(self) -> None:
        text = FULL_REGRESSION.read_text(encoding="utf-8")
        self.assertIn("CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", text)
        self.assertIn("ref: ${{ env.CANDIDATE_SHA }}", text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', text)

    def test_full_regression_runs_for_every_main_push(self) -> None:
        text = FULL_REGRESSION.read_text(encoding="utf-8")
        self.assertIn('push:\n    branches: ["main"]\n  pull_request:', text)
        self.assertNotIn('push:\n    branches: ["main"]\n    paths:', text)

    def test_receipt_binds_runner_engine_dependencies_and_workflow(self) -> None:
        text = FULL_REGRESSION.read_text(encoding="utf-8")
        for needle in (
            '"schema": "isco-canonical-full-regression-v1"',
            '"runner_sha": candidate',
            '"engine_sha": os.environ["ENGINE_SHA"]',
            '"requirements_lock_sha256": sha256("engine/requirements-lock.txt")',
            '"workflow_sha256": sha256(".github/workflows/verify-private-engine.yml")',
            '"full_engine": "green"',
            '"full_runner": "green"',
            '"status": "green"',
            '"production_dispatch_performed": False',
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_main_only_full_regression_tag_is_exact_sha(self) -> None:
        text = FULL_REGRESSION.read_text(encoding="utf-8")
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", text)
        self.assertIn('tag="full-regression-green-$CANDIDATE_SHA"', text)
        self.assertIn('test "$(jq -r .object.sha <<<"$payload")" = "$CANDIDATE_SHA"', text)

    def test_stage_ladder_remains_independent_exact_sha_evidence(self) -> None:
        text = STAGE_LADDER.read_text(encoding="utf-8")
        self.assertIn("CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", text)
        self.assertIn('tag="stage-ladder-green-$CANDIDATE_SHA"', text)

    def test_production_fast_path_is_not_enabled_in_t3(self) -> None:
        text = PRODUCTION.read_text(encoding="utf-8")
        self.assertIn("python -m unittest discover -s tests -q", text)
        self.assertIn("find scripts -maxdepth 1 -type f -name 'test_*.py'", text)
        self.assertNotIn("full-regression-green-$GITHUB_SHA", text)


if __name__ == "__main__":
    unittest.main()
