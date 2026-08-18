from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
from scripts.planner_quality_guard import (
    _QUESTION_ANSWER_RUNTIME_RULE,
    _single_use_transition_slots,
    install_planner_quality_guard,
)


class PlannerQualityGuardTests(unittest.TestCase):
    def test_film_transition_hints_are_never_recycled(self) -> None:
        slots = _single_use_transition_slots(["أ", "ب", "ج"], 8)
        self.assertEqual(slots, ["أ", "ب", "ج", "", "", "", ""])
        self.assertEqual(slots.count("أ"), 1)
        self.assertEqual(slots.count("ب"), 1)
        self.assertEqual(slots.count("ج"), 1)

    def test_story_transition_hints_are_never_recycled(self) -> None:
        self.assertEqual(
            _single_use_transition_slots(["أ", "ب", "ج"], 5),
            ["أ", "ب", "ج", ""],
        )

    def test_empty_transition_values_are_not_forced(self) -> None:
        self.assertEqual(
            _single_use_transition_slots(["أ", "", "  ", "ب"], 6),
            ["أ", "ب", "", "", ""],
        )

    def test_question_answer_rule_requires_spoken_structure_not_metadata_only(self) -> None:
        self.assertIn("SPOKEN narration itself", _QUESTION_ANSWER_RUNTIME_RULE)
        self.assertIn("do not collapse", _QUESTION_ANSWER_RUNTIME_RULE)
        self.assertIn("metadata", _QUESTION_ANSWER_RUNTIME_RULE)

    def test_installed_wrapper_passes_single_use_slots_without_extra_call(self) -> None:
        calls: list[dict] = []

        def fake_write(*args, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

        with patch.object(staged, "_write_full_script", fake_write):
            install_planner_quality_guard()
            result = staged._write_full_script(
                "key",
                topic="موضوع",
                fmt="film",
                model="model",
                briefs=[{"id": f"s{i}"} for i in range(1, 9)],
                narrative_format="question_answer",
                target_per_section=120,
                transition_variants=["أ", "ب", "ج"],
                research_json="{}",
                avoid_json="{}",
                policy_json="{}",
                revision_note="",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["transition_variants"], ["أ", "ب", "ج", "", "", "", ""])


if __name__ == "__main__":
    unittest.main()
