from __future__ import annotations

import unittest

from isco_video_agent import adaptive_outline_contract as engine_contract
from isco_video_agent.resilient_planner import build_outline_structure_prompt

from scripts import planning_envelope_preflight as preflight
from scripts import planning_outline_split_contract as split_contract
from scripts import planning_provider_visible_semantics as provider_semantics
from scripts import task_level_planner_router as router


class AdaptiveCoreEnvelopeParityTests(unittest.TestCase):
    def _engine_core_prompt(self, fmt: str) -> str:
        prompt = build_outline_structure_prompt(
            topic="موضوع تجريبي",
            fmt=fmt,
            policy_json="{}",
            research_json="{}",
            avoid_json="{}",
            learning_json="{}",
            revision_note="",
        )
        if engine_contract.GLOBAL_SECTION_SKELETON_MARKER not in prompt:
            self.skipTest(
                "installed production Engine predates Engine-owned adaptive Core rendering"
            )
        return prompt

    def test_certificate_and_runtime_render_exact_same_provider_visible_core(self) -> None:
        for fmt, expected_count in (("film", 8), ("story", 5)):
            with self.subTest(fmt=fmt):
                core_prompt = self._engine_core_prompt(fmt)
                self.assertEqual(
                    core_prompt.count(engine_contract.GLOBAL_SECTION_SKELETON_MARKER),
                    1,
                )

                spec = split_contract.outline_core_stage_spec_for_format(fmt)

                runtime_prompt = engine_contract.core_prompt_with_global_skeleton(
                    split_contract.core_portability_prompt(core_prompt),
                    expected_count,
                )
                runtime_provider_visible = router.with_channel_persona(
                    router._enrich_dialogue_prompt(
                        provider_semantics._provider_visible_prompt(runtime_prompt, spec)
                    )
                )

                certified_provider_visible = preflight._effective_split_provider_prompt(
                    core_prompt,
                    spec,
                    core=True,
                )

                self.assertEqual(certified_provider_visible, runtime_provider_visible)
                self.assertEqual(
                    certified_provider_visible.count(
                        engine_contract.GLOBAL_SECTION_SKELETON_MARKER
                    ),
                    1,
                )

    def test_core_completion_and_operational_capacity_guards_are_unchanged(self) -> None:
        spec = split_contract.outline_core_stage_spec_for_format("film")
        self.assertEqual(spec.provider_policy.completion_tokens, 2400)
        self.assertTrue(spec.provider_policy.second_pass_after_full_exhaustion)


if __name__ == "__main__":
    unittest.main()
