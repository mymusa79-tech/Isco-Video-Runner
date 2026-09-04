from __future__ import annotations

import unittest

from isco_video_agent.short_professional_intelligence import choose_length_band

from scripts import native_short_stage_contract as short_contract
from scripts import planning_stage_contract as planning


class Run187MomentDurationContractClosureTests(unittest.TestCase):
    TOPIC = "كيف تنهض عندما تفقد الدافع تمامًا؟"

    def _plan(self, expected_seconds: float) -> dict:
        return {
            "topic": self.TOPIC,
            "pillar": "rise",
            "format": "moment",
            "hook": "حين يختفي الدافع، ماذا يبقى؟",
            "title_options": ["العنوان أ", "العنوان ب", "العنوان ج"],
            "thumbnail_concepts": ["concept a", "concept b", "concept c"],
            "sections": [
                {
                    "id": "s1",
                    "narration": "",
                    "visual_query": "person sitting quietly near window morning light",
                    "on_screen_text": "لا تنتظر عودة الدافع",
                    "emotion": "reflective",
                    "expected_seconds": expected_seconds,
                    "key_point": "ابدأ بحركة صغيرة قبل عودة الشعور",
                }
            ],
            "cta": "",
            "closing_payoff": "الحركة الصغيرة قد تسبق الشعور.",
        }

    def setUp(self):
        short_contract._install_stage_contract_extensions()

    def test_stage_contract_uses_professional_12_to_20_second_intersection(self):
        spec = short_contract.moment_stage_spec("short_draft", self.TOPIC)
        self.assertEqual(spec.semantic_rules["expected_seconds_min"], 12.0)
        self.assertEqual(spec.semantic_rules["expected_seconds_max"], 20.0)

    def test_run187_ten_second_plan_is_rejected_before_render(self):
        spec = short_contract.moment_stage_spec("short_draft", self.TOPIC)
        bound = planning.bind_request_contract(spec, "effective prompt")
        with self.assertRaises(planning.PlanningStageError) as captured:
            planning.validate_response(bound, self._plan(10.0))
        self.assertEqual(captured.exception.code, planning.PlanningErrorCode.SEMANTIC_INVALID)
        self.assertIn("outside_12_20_seconds", str(captured.exception))

    def test_twelve_second_boundary_remains_valid(self):
        spec = short_contract.moment_stage_spec("short_draft", self.TOPIC)
        bound = planning.bind_request_contract(spec, "effective prompt")
        result = planning.validate_response(bound, self._plan(12.0))
        self.assertEqual(result["sections"][0]["expected_seconds"], 12.0)

    def test_provider_visible_prompt_no_longer_advertises_7_second_moments(self):
        prompt = (
            "Return JSON. duration guidance: moment 7-20 seconds, short 20-45 seconds."
        )
        effective = short_contract._provider_visible_moment_duration_contract(prompt)
        self.assertNotIn("moment 7-20 seconds", effective)
        self.assertIn("moment 12-20 seconds", effective)
        self.assertIn("sections[0].expected_seconds MUST be between 12 and 20", effective)

    def test_review_or_repair_prompt_without_legacy_phrase_still_gets_contract(self):
        effective = short_contract._provider_visible_moment_duration_contract(
            "Review this candidate and return corrected JSON."
        )
        self.assertIn("sections[0].expected_seconds MUST be between 12 and 20", effective)

    def test_professional_gate_is_not_lowered_or_bypassed(self):
        rejected = choose_length_band(estimated_spoken_seconds=10.0, beat_count=2)
        accepted = choose_length_band(estimated_spoken_seconds=12.0, beat_count=2)
        self.assertFalse(rejected["length_fit_pass"])
        self.assertEqual(rejected["reason"], "estimated_duration_below_professional_floor")
        self.assertTrue(accepted["length_fit_pass"])
        self.assertEqual(accepted["band"], "micro")


if __name__ == "__main__":
    unittest.main()
