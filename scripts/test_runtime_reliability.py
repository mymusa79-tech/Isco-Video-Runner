from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.orchestrator as orchestrator
from scripts.runtime_reliability import (
    FailureClass,
    assert_runtime_contracts,
    classify_failure,
    write_failure_envelope,
)


class RuntimeReliabilityTests(unittest.TestCase):
    def test_failure_classes_have_single_explicit_owner(self) -> None:
        cases = [
            ("HTTP 429 quota exceeded", FailureClass.PROVIDER_OVERLOAD, "provider_router"),
            ("request timed out", FailureClass.TRANSIENT_PROVIDER, "provider_router"),
            ("HTTP 401 Unauthorized", FailureClass.PERMANENT_CONFIG, "preflight_or_provider_router"),
            ("Provider returned invalid JSON", FailureClass.SCHEMA_INVALID, "schema_repair"),
            ("required final section band is 110-170", FailureClass.SEMANTIC_INVALID, "bounded_output_recovery"),
            ("AI budget authorization denied", FailureClass.BUDGET_EXHAUSTED, "budget_ledger"),
            ("Visual QA found no safe/relevant candidate", FailureClass.RESOURCE_UNAVAILABLE, "visual_recovery"),
            ("Runtime contract failed: marker missing", FailureClass.RUNTIME_CONTRACT, "runtime_preflight"),
            ("ffmpeg decode failed", FailureClass.MEDIA_FAILURE, "media_pipeline"),
            ("Content-quality gate blocked real production", FailureClass.QUALITY_BLOCK, "quality_gate"),
        ]
        for detail, category, owner in cases:
            with self.subTest(detail=detail):
                policy = classify_failure(RuntimeError(detail))
                self.assertEqual(policy.failure_class, category)
                self.assertEqual(policy.owner, owner)

    def test_unknown_is_fail_closed_not_retryable(self) -> None:
        policy = classify_failure(RuntimeError("never-seen-before failure"))
        self.assertEqual(policy.failure_class, FailureClass.UNEXPECTED)
        self.assertFalse(policy.retryable)
        self.assertEqual(policy.owner, "fail_closed")

    def test_failure_envelope_is_atomic_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "plan.json").write_text("{}", encoding="utf-8")
            old = dict(os.environ)
            try:
                os.environ["GITHUB_SHA"] = "a" * 40
                os.environ["ISCO_ENGINE_SHA"] = "b" * 40
                os.environ["GEMINI_API_KEY"] = "must-not-appear"
                target = write_failure_envelope(
                    out,
                    stage="core_production",
                    exc=RuntimeError("HTTP 503 server error"),
                    production_id="v4:test:1",
                )
            finally:
                os.environ.clear()
                os.environ.update(old)
            self.assertTrue(target.is_file())
            self.assertFalse((out / "failure-envelope.json.tmp").exists())
            raw = target.read_text(encoding="utf-8")
            self.assertNotIn("must-not-appear", raw)
            data = json.loads(raw)
            self.assertEqual(data["failure_class"], "transient_provider")
            self.assertEqual(data["stage"], "core_production")
            self.assertIn("plan.json", data["artifacts_present"])

    def test_runtime_contract_detects_router_marker_loss_before_provider_call(self) -> None:
        def unmarked(*args, **kwargs):
            del args, kwargs

        with patch.object(orchestrator, "build_plan", unmarked):
            with self.assertRaisesRegex(RuntimeError, "orchestrator.build_plan missing marker"):
                assert_runtime_contracts()


if __name__ == "__main__":
    unittest.main()
