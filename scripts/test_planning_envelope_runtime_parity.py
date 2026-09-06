from __future__ import annotations

import unittest
from unittest import mock

from scripts import planning_envelope_preflight as preflight
from scripts import planning_outline_split_contract as split
from scripts import planning_provider_visible_semantics as visible
from scripts import producer_quality_contract as producer


class PlanningEnvelopeRuntimeParityTests(unittest.TestCase):
    def test_long_preflight_includes_exact_live_producer_revision_in_both_split_calls(self) -> None:
        research = {
            "approved_research_pack": [{"claim": "approved"}],
            "content_boundaries": ["stay within approved evidence"],
        }
        expected_revision = producer.merge_producer_revision_note("", research)
        seen: dict[str, str] = {}

        def core_builder(**kwargs):
            seen["core"] = kwargs["revision_note"]
            return "core-prompt"

        def sections_builder(**kwargs):
            seen["sections"] = kwargs["revision_note"]
            return "sections-prompt"

        premise = {
            "narrative_format": "direct_cinematic",
            "pillar": "understand",
            "hook": "hook",
            "closing_payoff": "payoff",
            "editorial_intent": {
                "editorial_thesis": "thesis",
                "viewer_starting_belief": "belief",
                "hidden_assumption": "assumption",
                "editorial_turn": "turn",
                "stakes": "stakes",
                "viewer_promise": "promise",
                "evidence_boundaries": ["boundary"],
                "earned_payoff": "earned",
            },
        }
        fake_capacity = {
            "estimated_request_tokens": 100,
            "provider_tpm_limit": 8000,
        }

        with mock.patch.object(preflight, "load_editorial_policy", return_value={}), \
                mock.patch.object(preflight, "novelty_context", return_value={}), \
                mock.patch.object(preflight, "learning_context", return_value={}), \
                mock.patch.object(preflight, "build_outline_structure_prompt", side_effect=core_builder), \
                mock.patch.object(preflight, "build_outline_sections_prompt", side_effect=sections_builder), \
                mock.patch.object(preflight, "_bounded_preflight_locked_premise", return_value=premise), \
                mock.patch.object(preflight, "_effective_split_provider_prompt", side_effect=lambda prompt, *_args, **_kwargs: prompt), \
                mock.patch.object(preflight, "groq_capacity_estimate", return_value=fake_capacity):
            preflight._split_outline_envelopes(
                brief={"approved_topic": "موضوع"},
                fmt="film",
                research=research,
            )

        self.assertEqual(seen["core"], expected_revision)
        self.assertEqual(seen["sections"], expected_revision)
        self.assertNotEqual(expected_revision, "")

    def test_producer_prompt_is_compact_but_deterministic_gates_remain_separate(self) -> None:
        directive = producer.producer_writing_directive(
            {"approved_research_pack": [{"claim": "approved"}]}
        )
        self.assertIn("APPROVED_RESEARCH_PACK", directive)
        self.assertIn("precise/high-risk", directive)
        self.assertIn("non-diagnostic", directive)
        self.assertIn("non-preachy", directive)
        self.assertIn("template progression", directive)
        self.assertLess(len(directive.encode("utf-8")), 360)
        self.assertTrue(callable(producer.validate_plan_for_producer_handoff))

    def test_provider_visible_pillar_guidance_is_compact_and_schema_remains_authoritative(self) -> None:
        spec = split.outline_core_stage_spec_for_format("film")
        prompt = visible._provider_visible_prompt("CORE", spec)
        self.assertIn(visible._VISIBLE_MARKER, prompt)
        for pillar in visible.PLANNING_PILLARS:
            self.assertIn(pillar, prompt)
        self.assertIn("translate/rename/invent", prompt)
        self.assertLess(len(prompt.encode("utf-8")), 150)

        contract = split.stage_contract.bind_request_contract(spec, "CORE")
        upgraded = visible._with_pillar_schema(contract)
        self.assertEqual(
            upgraded.output_schema["properties"]["pillar"]["enum"],
            list(visible.PLANNING_PILLARS),
        )


class PlanningSplitRetryPolicyTests(unittest.TestCase):
    def test_split_policy_keeps_provider_first_single_attempt_sweep(self) -> None:
        for spec in (
            split.outline_core_stage_spec_for_format("film"),
            split.outline_sections_stage_spec_for_format("film"),
        ):
            policy = spec.provider_policy
            self.assertEqual(policy.max_attempts_per_provider, 1)
            self.assertEqual(policy.max_total_attempts, 6)
            self.assertEqual(policy.completion_tokens, split._COMPLETION_TOKENS)
            self.assertEqual(
                policy.completion_tokens_for("gemini"),
                split._GEMINI_COMPLETION_TOKENS,
            )
            self.assertTrue(policy.second_pass_after_full_exhaustion)


if __name__ == "__main__":
    unittest.main()
