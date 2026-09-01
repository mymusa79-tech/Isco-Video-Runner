from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as groq_pool
from scripts import short_initial_planning as short


MODEL = "openai/gpt-oss-120b"


def _moment_payload(topic: str = "كيف تنهض عندما تفقد الدافع تمامًا؟") -> dict:
    return {
        "topic": topic,
        "pillar": "rise",
        "format": "moment",
        "hook": "حين يختفي الدافع، لا تنتظر عودته.",
        "title_options": [topic, "لا تنتظر الدافع", "ابدأ قبل أن تشعر"],
        "thumbnail_concepts": ["شخص ينهض من كرسي في ضوء صباحي واقعي"],
        "sections": [
            {
                "id": "s1",
                "narration": "",
                "visual_query": "person standing up from chair morning light realistic modest clothing",
                "on_screen_text": "لا تنتظر الدافع",
                "emotion": "hopeful",
                "expected_seconds": 12,
                "key_point": "ابدأ بخطوة صغيرة قبل أن يعود الشعور بالدافع.",
            }
        ],
        "cta": "",
        "closing_payoff": "ابدأ بخطوة صغيرة، ثم دع الحركة تعيد الدافع.",
    }


class ShortInitialPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        capacity.reset_groq_capacity_state_for_tests()
        self.original_index = groq_pool._ACTIVE_GROQ_INDEX

    def tearDown(self) -> None:
        groq_pool._ACTIVE_GROQ_INDEX = self.original_index
        capacity.reset_groq_capacity_state_for_tests()

    def test_huge_context_is_compacted_and_routed_request_stays_below_certified_envelope(self):
        huge = "سياق طويل " * 3000
        prompt = short.build_short_initial_prompt(
            "كيف تنهض عندما تفقد الدافع تمامًا؟",
            research_context={
                "approved_audience": huge,
                "approved_editorial_direction": huge,
                "content_boundaries": [huge] * 20,
                "factuality_rule": huge,
                "approved_research_pack": [
                    {"source_title": huge, "claim_scope": huge, "evidence": huge}
                    for _ in range(20)
                ],
            },
            avoid_context={"recent_hooks": [huge] * 20, "recent_visuals": [huge] * 20},
            revision_note=huge,
        )
        self.assertLessEqual(len(prompt.encode("utf-8")), short.SHORT_INITIAL_PROMPT_MAX_BYTES)
        estimate = short.short_routed_capacity_estimate(prompt)
        self.assertLessEqual(
            estimate["estimated_request_tokens"],
            short.SHORT_INITIAL_MAX_ROUTED_REQUEST_TOKENS,
        )

    def test_initial_moment_keeps_two_pass_draft_review_and_template_directive(self):
        topic = "كيف تنهض عندما تفقد الدافع تمامًا؟"
        revision = "Standalone Short type is inner_dialogue. Preserve the internal tension."
        payload = _moment_payload(topic)
        with patch.object(short.native_short, "json_text", side_effect=[payload, payload]) as provider:
            plan = short._build_initial_moment(
                "test-key",
                topic,
                "gemini-test",
                research_context={"content_boundaries": ["لا تشخيص طبي"]},
                avoid_context={"recent_hooks": ["قديم"]},
                revision_note=revision,
            )
        self.assertEqual(provider.call_count, 2)
        first_prompt = provider.call_args_list[0].args[1]
        second_prompt = provider.call_args_list[1].args[1]
        self.assertIn(revision, first_prompt)
        self.assertIn(revision, second_prompt)
        self.assertEqual(plan.format, "moment")
        self.assertEqual(len(plan.sections), 1)
        self.assertEqual(plan.sections[0].narration, "")

    def test_terminal_reset_candidate_is_bounded_to_capacity_only_and_short_wait(self):
        prompt = short.build_short_initial_prompt(
            "كيف تنهض عندما تفقد الدافع تمامًا؟",
            research_context={},
            avoid_context={},
            revision_note="Standalone Short type is inner_dialogue.",
        )
        estimate = short.short_routed_capacity_estimate(prompt)
        required = int(estimate["estimated_request_tokens"])
        groq_pool._ACTIVE_GROQ_INDEX = 1
        state = capacity._model_state(MODEL)
        state.update(
            {
                "contacted": True,
                "actual_tpm_limit": 8000,
                "remaining_tokens": max(0, required - 100),
                "reset_at_epoch": time.time() + 1.0,
                "blocked_reason": None,
            }
        )
        attempts = [
            {"provider": "gemini", "result": "429"},
            {"provider": "groq", "result": "capacity_wait"},
            {"provider": "openrouter", "result": "429"},
        ]
        budget = short._ShortResetBudget()
        candidate = short._terminal_reset_candidate(
            prompt,
            phase="draft",
            attempts=attempts,
            budget=budget,
        )
        self.assertIsNotNone(candidate)
        model, delay = candidate
        self.assertEqual(model, MODEL)
        self.assertGreater(delay, 1.0)
        self.assertLessEqual(delay, short.SHORT_INITIAL_RESET_WAIT_MAX_SECONDS)

        non_capacity = attempts + [{"provider": "gemini", "result": "generation_error"}]
        self.assertIsNone(
            short._terminal_reset_candidate(
                prompt,
                phase="review",
                attempts=non_capacity,
                budget=short._ShortResetBudget(),
            )
        )

    def test_long_reset_is_not_waited_by_short_terminal_owner(self):
        prompt = short.build_short_initial_prompt(
            "كيف تنهض عندما تفقد الدافع تمامًا؟",
            research_context={},
            avoid_context={},
            revision_note="Standalone Short type is inner_dialogue.",
        )
        estimate = short.short_routed_capacity_estimate(prompt)
        required = int(estimate["estimated_request_tokens"])
        groq_pool._ACTIVE_GROQ_INDEX = 1
        state = capacity._model_state(MODEL)
        state.update(
            {
                "contacted": True,
                "actual_tpm_limit": 8000,
                "remaining_tokens": max(0, required - 100),
                "reset_at_epoch": time.time() + 30.0,
                "blocked_reason": None,
            }
        )
        attempts = [{"provider": "groq", "result": "capacity_wait"}]
        self.assertIsNone(
            short._terminal_reset_candidate(
                prompt,
                phase="draft",
                attempts=attempts,
                budget=short._ShortResetBudget(),
            )
        )


if __name__ == "__main__":
    unittest.main()
