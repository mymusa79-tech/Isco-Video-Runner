from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import isco_video_agent.orchestrator as orchestrator

from scripts import production_failure_diagnostics as failure_diag
from scripts import text_audit_outcome_contract as contract


class TextAuditOutcomeContractTests(unittest.TestCase):
    def test_real_semantic_pass_and_block_are_preserved(self):
        passed = {"status": "pass", "validation": "valid"}
        blocked = {"status": "block", "validation": "valid", "naturalness_flags": ["real defect"]}
        self.assertIs(contract.enforce_text_audit_outcome("tone", passed), passed)
        self.assertIs(contract.enforce_text_audit_outcome("tone", blocked), blocked)

    def test_provider_exhaustion_is_technical_not_repairable_content_block(self):
        with self.assertRaises(contract.TextAuditOutcomeError) as captured:
            contract.enforce_text_audit_outcome(
                "semantic_repetition",
                {"status": "block", "validation": "providers_exhausted", "duplicate_groups": []},
            )
        self.assertEqual(captured.exception.code, contract.PROVIDER_EXHAUSTED)
        self.assertEqual(captured.exception.dimension, "semantic_repetition")

    def test_malformed_audit_is_technical_and_fail_closed(self):
        with self.assertRaises(contract.TextAuditOutcomeError) as captured:
            contract.enforce_text_audit_outcome(
                "tone",
                {"status": "block", "validation": "malformed"},
            )
        self.assertEqual(captured.exception.code, contract.INVALID_MODEL_OUTPUT)

    def test_factuality_wrapper_injects_diagnostics_and_sees_provider_exhaustion(self):
        seen = {}

        def fake(*_args, **kwargs):
            diagnostics = kwargs["diagnostics"]
            seen["same"] = diagnostics
            diagnostics.update({"validation": "providers_exhausted"})
            return {"status": "block", "unsupported_claims": ["synthetic technical flag"]}

        wrapped = contract._wrap_audit(fake, dimension="factuality", factuality_diagnostics=True)
        with self.assertRaises(contract.TextAuditOutcomeError) as captured:
            wrapped("k", object(), {}, "model")
        self.assertEqual(captured.exception.code, contract.PROVIDER_EXHAUSTED)
        self.assertIsInstance(seen["same"], dict)

    def test_install_wraps_all_three_live_orchestrator_boundaries(self):
        originals = (
            orchestrator.audit_plan,
            orchestrator.audit_semantic_repetition,
            orchestrator.audit_tone_and_naturalness,
        )
        old_installed = contract._INSTALLED
        try:
            contract._INSTALLED = False
            contract.install_text_audit_outcome_contract()
            self.assertTrue(getattr(orchestrator.audit_plan, "_isco_text_audit_outcome_v1", False))
            self.assertTrue(getattr(orchestrator.audit_semantic_repetition, "_isco_text_audit_outcome_v1", False))
            self.assertTrue(getattr(orchestrator.audit_tone_and_naturalness, "_isco_text_audit_outcome_v1", False))
        finally:
            orchestrator.audit_plan, orchestrator.audit_semantic_repetition, orchestrator.audit_tone_and_naturalness = originals
            contract._INSTALLED = old_installed


class ProductionFailureDiagnosticsTests(unittest.TestCase):
    def test_text_audit_technical_failure_never_claims_tone_semantic_block(self):
        exc = contract.TextAuditOutcomeError(contract.PROVIDER_EXHAUSTED, "tone")
        self.assertFalse(failure_diag.is_tone_semantic_failure(exc))
        category, code = failure_diag.classify_production_failure(exc)
        self.assertEqual((category, code), ("text_audit", contract.PROVIDER_EXHAUSTED))

    def test_real_tone_gate_remains_identifiable(self):
        exc = RuntimeError("Independent tone/naturalness gate blocked real production")
        self.assertTrue(failure_diag.is_tone_semantic_failure(exc))
        self.assertEqual(
            failure_diag.classify_production_failure(exc),
            ("text_audit", "TONE_SEMANTIC_BLOCK"),
        )

    def test_failure_file_is_secret_free_and_does_not_persist_raw_message(self):
        secret = "SECRET-DO-NOT-PERSIST"
        exc = RuntimeError(f"429 provider URL key={secret}")
        with tempfile.TemporaryDirectory() as td:
            path = failure_diag.write_production_failure_diagnostics(Path(td), exc)
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertFalse(payload["raw_exception_persisted"])
        self.assertEqual(payload["category"], "provider")
        self.assertEqual(payload["error_code"], "PROVIDER_CAPACITY_FAILURE")


if __name__ == "__main__":
    unittest.main()
