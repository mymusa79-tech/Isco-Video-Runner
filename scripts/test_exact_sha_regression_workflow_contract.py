from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "verify-private-engine.yml"


class ExactSHARegressionWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_pull_request_checkout_is_bound_to_head_sha(self) -> None:
        self.assertIn("CANDIDATE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", self.text)
        self.assertIn("ref: ${{ env.CANDIDATE_SHA }}", self.text)
        self.assertIn('test "$(git rev-parse HEAD)" = "$CANDIDATE_SHA"', self.text)

    def test_main_push_is_not_path_filtered(self) -> None:
        push_start = self.text.index("  push:\n")
        pr_start = self.text.index("  pull_request:\n", push_start)
        push_block = self.text[push_start:pr_start]
        self.assertIn('branches: ["main"]', push_block)
        self.assertNotIn("paths:", push_block)
        self.assertNotIn("paths-ignore:", push_block)

    def test_receipt_is_built_only_after_full_runner_and_closure(self) -> None:
        full_runner = self.text.index("      - name: Full Runner regression suite")
        closure = self.text.index("      - name: Exact closure delta sanity")
        receipt = self.text.index("      - name: Build exact-SHA canonical regression receipt")
        self.assertLess(full_runner, closure)
        self.assertLess(closure, receipt)
        self.assertIn("scripts/exact_sha_regression_receipt.py create", self.text)
        self.assertIn("scripts/exact_sha_regression_receipt.py validate", self.text)

    def test_durable_ref_is_main_push_only_and_binds_both_shas(self) -> None:
        self.assertIn("if: github.event_name == 'push' && github.ref == 'refs/heads/main'", self.text)
        self.assertIn('tag="canonical-full-regression-green-$CANDIDATE_SHA-$ENGINE_SHA"', self.text)
        self.assertIn('test "$(jq -r .object.sha <<<"$payload")" = "$CANDIDATE_SHA"', self.text)

    def test_receipt_is_uploaded_as_evidence(self) -> None:
        self.assertIn("Upload exact-SHA canonical regression receipt", self.text)
        self.assertIn("retention-days: 30", self.text)


if __name__ == "__main__":
    unittest.main()
