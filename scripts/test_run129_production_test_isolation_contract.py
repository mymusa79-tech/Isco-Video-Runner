from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_WORKFLOW = ROOT / ".github" / "workflows" / "produce-resilient-v4.yml"
VERIFY_WORKFLOW = ROOT / ".github" / "workflows" / "verify-private-engine.yml"
M7_WORKFLOW = ROOT / ".github" / "workflows" / "verify-human-editorial-intent-m7.yml"
M11_WORKFLOW = ROOT / ".github" / "workflows" / "verify-m11-live-integration.yml"


class Run129ProductionTestIsolationContractTests(unittest.TestCase):
    def test_production_engine_suite_uses_test_owned_history(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("production-engine-suite/history.json", text)
        self.assertRegex(
            text,
            re.compile(
                r'ISCO_HISTORY_PATH="\$engine_test_history"\s+python -m unittest discover -s tests -q'
            ),
        )
        self.assertIn('certify_engine_source_hermeticity("production_after_engine_suite")', text)

    def test_production_runner_suite_uses_separate_test_owned_history(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "ISCO_HISTORY_PATH: ${{ runner.temp }}/isco-test-state/production-runner-suite/history.json",
            text,
        )
        self.assertIn('certify_engine_source_hermeticity("production_after_runner_suite")', text)

    def test_production_fails_closed_before_provider_work(self) -> None:
        text = PRODUCTION_WORKFLOW.read_text(encoding="utf-8")
        gate = 'certify_engine_source_hermeticity("production_before_provider_work")'
        provider_step = "      - name: Materialize approved production secrets"
        self.assertIn(gate, text)
        self.assertIn(provider_step, text)
        self.assertLess(text.index(gate), text.index(provider_step))

    def test_verify_workflow_keeps_phase_specific_test_state(self) -> None:
        text = VERIFY_WORKFLOW.read_text(encoding="utf-8")
        required = (
            "verify-run126-history.json",
            "verify-run104-history.json",
            "verify-full-engine-history.json",
            "verify-approved-brief-cli-history.json",
            "verify-short-v2-history.json",
            "verify-full-runner-history.json",
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_m7_workflow_isolates_engine_and_runner_state(self) -> None:
        text = M7_WORKFLOW.read_text(encoding="utf-8")
        required = (
            "m7-focused-engine-history.json",
            "m7-full-engine-history.json",
            "m7-approved-brief-cli-history.json",
            "m7-focused-runner-history.json",
            "m7-full-runner-history.json",
            'certify_engine_source_hermeticity("m7_after_full_engine")',
            'certify_engine_source_hermeticity("m7_after_approved_brief_cli")',
            'certify_engine_source_hermeticity("m7_after_full_runner")',
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_m11_workflow_isolates_engine_and_runner_state(self) -> None:
        text = M11_WORKFLOW.read_text(encoding="utf-8")
        required = (
            "m11-focused-engine-history.json",
            "m11-renderer-smoke-history.json",
            "m11-full-engine-history.json",
            "m11-focused-runner-history.json",
            "m11-full-runner-history.json",
            'certify_engine_source_hermeticity("m11_after_full_engine")',
            'certify_engine_source_hermeticity("m11_after_full_runner")',
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_no_cleanup_based_escape_hatch_is_introduced(self) -> None:
        forbidden = (
            "git reset --hard",
            "git checkout -- engine/",
            "git restore engine/",
        )
        for workflow in (PRODUCTION_WORKFLOW, M7_WORKFLOW, M11_WORKFLOW):
            text = workflow.read_text(encoding="utf-8")
            for marker in forbidden:
                with self.subTest(workflow=workflow.name, marker=marker):
                    self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
