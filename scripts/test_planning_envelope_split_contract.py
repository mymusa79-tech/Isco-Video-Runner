from __future__ import annotations

import unittest
from unittest import mock

from scripts import planning_envelope_preflight as preflight


class PlanningEnvelopeSplitContractTests(unittest.TestCase):
    def test_long_preflight_gates_on_larger_real_split_request(self) -> None:
        brief = {
            "approved_topic": "موضوع",
            "format": "film",
            "research_pack": [],
        }
        research = {
            "approved_research_pack": [],
            "content_boundaries": [],
        }
        core = {
            "estimated_request_tokens": 5100,
            "provider_tpm_limit": 8000,
            "reserved_completion_tokens": 2400,
        }
        sections = {
            "estimated_request_tokens": 6200,
            "provider_tpm_limit": 8000,
            "reserved_completion_tokens": 2400,
        }
        captured: dict[str, int] = {}

        def require(required_tokens, *, phase, required_families):
            captured["required_tokens"] = required_tokens
            self.assertEqual(phase, "preproduction_split_outline_envelope")
            self.assertEqual(required_families, preflight.P0_OUTLINE_MIN_PROVIDER_FAMILIES)
            return ["gemini", "groq:openai/gpt-oss-20b"], ("gemini", "groq")

        with (
            mock.patch.object(preflight, "audit_media_capacity_margin"),
            mock.patch.object(preflight, "load_approved_brief", return_value=brief),
            mock.patch.object(preflight, "planning_research_context", return_value=research),
            mock.patch.object(
                preflight,
                "_split_outline_envelopes",
                return_value=(core, sections, 12000, 14500),
            ),
            mock.patch.object(preflight, "_require_provider_redundancy", side_effect=require),
        ):
            result = preflight.certify_planning_envelope()

        self.assertEqual(captured["required_tokens"], 6200)
        self.assertEqual(result.outline_estimated_request_tokens, 6200)
        self.assertEqual(result.prompt_utf8_bytes, 14500)
        self.assertEqual(result.outline_groq_tpm_headroom, 1800)
        self.assertIn("split_outline_max_of_core_sections", result.runtime_token_admission)


if __name__ == "__main__":
    unittest.main()
