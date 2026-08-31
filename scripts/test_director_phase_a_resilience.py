from __future__ import annotations

import unittest

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.editorial_room import EditorialContractError

from scripts.director_phase_a_resilience import install_director_phase_a_resilience


class _FakePlan:
    def __init__(self, *, editorial_intent):
        self.topic = "موضوع اختبار"
        self.format = "moment"
        self.narrative_format = "short_why_reframe"
        self.editorial_intent = editorial_intent


class DirectorPhaseAResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._original = orchestrator.failed_observation_documents
        self.addCleanup(setattr, orchestrator, "failed_observation_documents", self._original)

    def test_real_function_still_raises_for_short_style_editorial_intent(self) -> None:
        # Regression baseline: confirms the underlying bug is real and unfixed in Engine
        # itself - native_short_planner_router.py's short_template/short_compensation_v2
        # metadata makes plan.editorial_intent non-empty, which director_brain.py then
        # validates as a full Editorial Room premise and rejects for missing thesis.
        plan = _FakePlan(
            editorial_intent={
                "short_template": "why_reframe",
                "short_compensation_v2": {"enabled": True},
            }
        )
        with self.assertRaisesRegex(EditorialContractError, "editorial_intent_thesis_missing_or_vague"):
            self._original(plan, "a" * 64, error_class="invalid_output")

    def test_guard_degrades_to_empty_observation_documents_instead_of_crashing(self) -> None:
        install_director_phase_a_resilience()
        plan = _FakePlan(
            editorial_intent={
                "short_template": "why_reframe",
                "short_compensation_v2": {"enabled": True},
            }
        )
        beat_plan, scene_plan = orchestrator.failed_observation_documents(
            plan, "a" * 64, error_class="invalid_output"
        )
        self.assertEqual(beat_plan["mode"], "observe_only")
        self.assertEqual(beat_plan["beats"], [])
        self.assertEqual(beat_plan["source"]["topic"], plan.topic)
        self.assertEqual(beat_plan["generation"]["error_class"], "invalid_output")
        self.assertEqual(scene_plan["mode"], "observe_only")
        self.assertEqual(scene_plan["scenes"], [])
        self.assertIsNone(scene_plan["visual_thesis"])

    def test_guard_is_transparent_for_the_normal_long_form_case(self) -> None:
        install_director_phase_a_resilience()
        plan = _FakePlan(editorial_intent=None)
        beat_plan, scene_plan = orchestrator.failed_observation_documents(
            plan, "b" * 64, error_class="timeout"
        )
        expected_beat_plan, expected_scene_plan = self._original(
            plan, "b" * 64, error_class="timeout"
        )
        self.assertEqual(beat_plan, expected_beat_plan)
        self.assertEqual(scene_plan, expected_scene_plan)

    def test_install_is_idempotent(self) -> None:
        install_director_phase_a_resilience()
        first = orchestrator.failed_observation_documents
        install_director_phase_a_resilience()
        self.assertIs(orchestrator.failed_observation_documents, first)


if __name__ == "__main__":
    unittest.main()
