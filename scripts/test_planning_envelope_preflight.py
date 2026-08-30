from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts.planning_envelope_preflight import certify_planning_envelope


ROOT = Path(__file__).resolve().parents[1]


class PlanningEnvelopePreflightTests(unittest.TestCase):
    def _certify_with_two_provider_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            preflight = Path(tmp) / "provider-preflight.json"
            preflight.write_text(
                json.dumps(
                    {
                        "checks": [
                            {"provider": "gemini", "status": "pass"},
                            {"provider": "groq", "status": "pass"},
                            {"provider": "openrouter", "status": "block"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"ISCO_PROVIDER_PREFLIGHT_PATH": str(preflight)},
            ):
                return certify_planning_envelope()

    def test_exact_pinned_engine_envelope_is_certified_without_inference(self) -> None:
        result = self._certify_with_two_provider_fixture()
        self.assertEqual(result.status, "pass")
        self.assertGreater(result.prompt_utf8_bytes, 0)
        self.assertGreater(result.remaining_headroom_utf8_bytes, 0)
        self.assertGreaterEqual(result.approved_sources, 2)
        self.assertGreater(result.approved_boundaries, 0)
        outline_spec = stage_contract.outline_stage_spec_for_format(result.format)
        writer_spec = stage_contract.script_stage_spec("full_script", ["s1", "s2", "s3"])
        self.assertEqual(
            result.outline_completion_reserve,
            outline_spec.provider_policy.completion_tokens,
        )
        self.assertEqual(
            result.full_script_completion_reserve,
            writer_spec.provider_policy.completion_tokens,
        )
        expected_prompt_tokens = math.ceil(
            result.prompt_utf8_bytes / capacity.GROQ_ESTIMATED_UTF8_BYTES_PER_TOKEN
        )
        self.assertEqual(
            result.outline_estimated_request_tokens,
            expected_prompt_tokens
            + result.outline_completion_reserve
            + capacity.GROQ_TOKEN_SAFETY_RESERVE,
        )
        self.assertEqual(result.outline_completion_reserve, 2400)
        self.assertEqual(result.full_script_completion_reserve, 1800)
        self.assertEqual(result.viable_provider_families, ("gemini", "groq"))
        self.assertEqual(result.required_provider_families, 2)
        self.assertIn("p0_two_provider_families", result.runtime_token_admission)

    def test_certification_runs_before_production(self) -> None:
        workflow = (ROOT / ".github/workflows/produce-resilient-v4.yml").read_text(
            encoding="utf-8"
        )
        certification = workflow.index("Certify provider-portable planning envelope")
        production = workflow.index("Produce with task-level brain and voice meshes")
        self.assertLess(certification, production)
        self.assertIn("scripts/planning_envelope_preflight.py", workflow)


if __name__ == "__main__":
    unittest.main()
