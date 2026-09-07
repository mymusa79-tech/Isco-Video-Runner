from __future__ import annotations

import inspect
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import planning_envelope_preflight as preflight_module
from scripts import planning_stage_contract as stage_contract
from scripts import producer_quality_contract
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

    def test_preflight_producer_revision_matches_runtime_format_scope(self) -> None:
        _, short_revision = preflight_module.compose_short_production_revision(
            "إرهاق اتخاذ القرارات",
            {},
        )
        self.assertIn("Moment one calm idea/beat", short_revision)
        self.assertIn("why_reframe", short_revision)
        self.assertNotIn("Long opening tension/promise", short_revision)

        for fmt in ("film", "story"):
            with self.subTest(fmt=fmt):
                long_revision = producer_quality_contract.merge_producer_revision_note(
                    "",
                    {},
                    fmt,
                )
                self.assertIn("Long opening tension/promise", long_revision)
                self.assertNotIn("Moment one calm idea/beat", long_revision)
                self.assertNotIn("why_reframe", long_revision)

        long_preflight_source = inspect.getsource(preflight_module._split_outline_envelopes)
        self.assertIn(
            'merge_producer_revision_note("", research, fmt)',
            long_preflight_source,
        )

    def test_certification_runs_before_production(self) -> None:
        workflow = (ROOT / ".github/workflows/produce-resilient-v4.yml").read_text(
            encoding="utf-8"
        )
        certification = workflow.index("Certify provider-portable planning envelope")
        production = workflow.index("id: produce_video")
        self.assertLess(certification, production)
        self.assertIn("scripts/planning_envelope_preflight.py", workflow)
        self.assertIn("Produce with canonical V4 runtime", workflow)


if __name__ == "__main__":
    unittest.main()