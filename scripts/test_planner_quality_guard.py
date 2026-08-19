from __future__ import annotations

import unittest
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged
from scripts.planner_quality_guard import (
    _QUESTION_ANSWER_RUNTIME_RULE,
    _safe_opening_visual_query,
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

    def test_run54_identifiable_person_query_is_reduced_to_objects_and_environment(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query(
                "person sitting by wooden table in sunlit room looking pensively at empty notebook"
            ),
            "table room notebook",
        )

    def test_explicit_non_identifiable_framing_is_preserved(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("hands writing notebook"),
            "hands writing notebook",
        )
        self.assertEqual(
            _safe_opening_visual_query("silhouette person walking road"),
            "silhouette person walking road",
        )

    def test_non_person_query_is_preserved(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("notebook desk window morning light"),
            "notebook desk window morning light",
        )

    def test_human_only_query_uses_safe_broad_fallback(self) -> None:
        self.assertEqual(
            _safe_opening_visual_query("person sitting alone thinking"),
            "quiet room natural light",
        )

    def test_installed_outline_wrapper_sanitizes_only_first_longform_brief_without_extra_call(self) -> None:
        outline_calls: list[dict] = []

        def fake_outline(*args, **kwargs):
            outline_calls.append(kwargs)
            return {
                "section_briefs": [
                    {
                        "id": "s1",
                        "visual_query": "person sitting by wooden table in sunlit room looking pensively at empty notebook",
                    },
                    {"id": "s2", "visual_query": "person walking city street"},
                ]
            }

        def fake_write(*args, **kwargs):
            return {"ok": True}

        with (
            patch.object(staged, "_outline", fake_outline),
            patch.object(staged, "_write_full_script", fake_write),
        ):
            install_planner_quality_guard()
            result = staged._outline("key", topic="موضوع", fmt="film", model="model")

        self.assertEqual(len(outline_calls), 1)
        self.assertEqual(result["section_briefs"][0]["visual_query"], "table room notebook")
        self.assertEqual(result["section_briefs"][1]["visual_query"], "person walking city street")

    def test_installed_wrapper_passes_single_use_slots_without_extra_call(self) -> None:
        calls: list[dict] = []

        def fake_outline(*args, **kwargs):
            return {"section_briefs": []}

        def fake_write(*args, **kwargs):
            calls.append(kwargs)
            return {"ok": True}

        with (
            patch.object(staged, "_outline", fake_outline),
            patch.object(staged, "_write_full_script", fake_write),
        ):
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
