from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import providers as providers_module
from clean_v2 import visual_qa as visual_qa_module
from clean_v2.pipeline import (
    _persist_planning_artifacts,
    _planning_prompt,
    _validate_plan_for_brief,
)
from clean_v2.visual_story import contextual_intent, validate_visual_story


def _brief(fmt: str = "film") -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": "كيف تبدأ بخطوة صغيرة",
        "format": fmt,
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": "شرح عملي متفائل وهادئ.",
        "research_pack": [],
        "hard_constraints": ["No fabricated facts."],
    }


def _planning_value(fmt: str = "film") -> dict:
    count = 3 if fmt == "short" else 5
    sections = [
        {
            "id": f"s{index}",
            "heading": f"قسم {index}",
            "purpose": f"يفهم المشاهد الفكرة {index}",
            "visual_query_en": f"warm notebook workspace action {index} no face",
            **(
                {"visual_query_alt_en": f"warm desk detail action {index} no face"}
                if fmt == "short"
                else {}
            ),
        }
        for index in range(1, count + 1)
    ]
    beats = [
        {
            "id": f"b{index}",
            "section_id": f"s{index}",
            "viewer_intent": f"يفهم المشاهد التحول {index}",
            "shot_intent": f"warm notebook workspace action {index} no face",
            "source_preference": "ai_still" if index == 2 else "stock_motion",
        }
        for index in range(1, count + 1)
    ]
    return {
        "title": "خطوة واحدة",
        "promise": "فهم بداية عملية قابلة للتطبيق",
        "cta": "" if fmt == "short" else "اكتب تجربتك في التعليقات.",
        "sections": sections,
        "visual_story": {
            "visual_world": (
                "Grounded hopeful cinematic realism, soft natural light, "
                "warm neutral colors, environments and hands, no identifiable faces."
            ),
            "story_arc": {
                "beginning": "friction is visible",
                "transformation": "one small action becomes clear",
                "arrival": "progress feels practical and earned",
            },
            "beats": beats,
        },
    }


class UnifiedVisualStoryPlanningTests(unittest.TestCase):
    def test_visual_story_json_is_built_from_planning_and_split_from_plan_json(self) -> None:
        brief = _brief("film")
        planned = _validate_plan_for_brief(_planning_value("film"), brief)
        self.assertIn("visual_story", planned)
        self.assertEqual(len(planned["visual_story"]["beats"]), 5)
        self.assertEqual(
            planned["visual_story"]["beats"][1]["source_preference"],
            "ai_still",
        )

        with tempfile.TemporaryDirectory() as root:
            output = Path(root)
            plan, story = _persist_planning_artifacts(output, planned)
            disk_plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            disk_story = json.loads(
                (output / "visual-story.json").read_text(encoding="utf-8")
            )

        self.assertNotIn("visual_story", plan)
        self.assertNotIn("visual_story", disk_plan)
        self.assertEqual(disk_story, story)
        self.assertEqual(
            set(disk_story),
            {"schema_version", "visual_world", "story_arc", "beats"},
        )
        self.assertEqual(
            set(disk_story["story_arc"]),
            {"beginning", "transformation", "arrival"},
        )
        self.assertEqual(disk_story["story_arc"]["beginning"], "friction is visible")
        self.assertEqual(
            disk_story["story_arc"]["transformation"],
            "one small action becomes clear",
        )
        self.assertEqual(
            disk_story["story_arc"]["arrival"],
            "progress feels practical and earned",
        )

    def test_same_visual_story_contract_is_valid_for_short_and_film(self) -> None:
        for fmt in ("short", "film"):
            with self.subTest(fmt=fmt):
                planned = _validate_plan_for_brief(_planning_value(fmt), _brief(fmt))
                story = validate_visual_story(planned["visual_story"], planned)
                self.assertEqual(len(story["beats"]), 3 if fmt == "short" else 5)
                self.assertTrue(
                    all(
                        beat["source_preference"] in {"stock_motion", "ai_still"}
                        for beat in story["beats"]
                    )
                )

    def test_planning_prompt_forbids_duration_driven_extra_beats(self) -> None:
        for fmt in ("short", "film", "podcast"):
            prompt = " ".join(_planning_prompt(_brief(fmt)).split())
            self.assertIn("Create a new beat ONLY when the idea", prompt)
            self.assertIn("NEVER invent extra beats to hit a", prompt)
            self.assertIn("DO NOT assume AI imagery is active", prompt)
            self.assertIn("6-14 useful search words", prompt)
            self.assertIn("hands only, back view, or objects only", prompt)

    def test_planning_and_recovery_keep_arab_muslim_visual_suitability_without_stereotypes(self) -> None:
        prompt = " ".join(_planning_prompt(_brief("podcast")).split())
        self.assertIn("credible contemporary Arab/Middle-Eastern environment", prompt)
        self.assertIn("Reject scenes centered on alcohol, gambling, nightclub/party", prompt)
        self.assertIn("Do NOT force mosques, prayer rugs", prompt)
        recovery = " ".join(
            visual_qa_module._alternate_visual_query_prompt(
                original_query="hands writing in notebook no face",
                narration_context="فكرة عملية عن بداية هادئة",
            ).split()
        )
        self.assertIn("culturally suitable for a broad Arab/Muslim audience", recovery)
        self.assertIn("avoid alcohol, gambling, nightclub/party imagery", recovery)
        self.assertIn("Do not force religious symbols", recovery)

    def test_mistral_planning_schema_requires_the_unified_story(self) -> None:
        schema = providers_module._mistral_planning_response_schema(
            _planning_prompt(_brief("film"))
        )
        self.assertIn("visual_story", schema["required"])
        story = schema["properties"]["visual_story"]
        self.assertEqual(
            story["required"],
            ["visual_world", "story_arc", "beats"],
        )
        self.assertEqual(
            story["properties"]["beats"]["items"]["properties"]["source_preference"]["enum"],
            ["stock_motion", "ai_still"],
        )


class UnifiedVisualStoryContextTests(unittest.TestCase):
    def test_contextual_intent_carries_previous_and_next_beats(self) -> None:
        story = {
            "beats": [
                {
                    "id": "b1",
                    "shot_intent": "closed notebook on a quiet desk",
                },
                {
                    "id": "b2",
                    "shot_intent": "hand opens notebook and writes one task",
                },
                {
                    "id": "b3",
                    "shot_intent": "checked task beside warm morning light",
                },
            ]
        }
        intent = contextual_intent(story, "b2", "fallback")
        self.assertIn("closed notebook", intent)
        self.assertIn("hand opens notebook", intent)
        self.assertIn("checked task", intent)
        self.assertIn("belongs naturally between its neighbors", intent)
        self.assertLessEqual(len(intent), 300)

    def test_contextual_intent_keeps_all_three_neighbors_when_shot_intents_are_long(self) -> None:
        story = {
            "beats": [
                {"id": "b1", "shot_intent": "previous " + ("detail " * 60)},
                {"id": "b2", "shot_intent": "current " + ("action " * 60)},
                {"id": "b3", "shot_intent": "next " + ("result " * 60)},
            ]
        }
        intent = contextual_intent(story, "b2", "fallback")
        self.assertIn("Current: current", intent)
        self.assertIn("Previous: previous", intent)
        self.assertIn("Next: next", intent)
        self.assertIn("Same story arc:", intent)
        self.assertTrue(intent.endswith("between its neighbors."))
        self.assertLessEqual(len(intent), 300)


class VisualSafetyRegressionTests(unittest.TestCase):
    def test_no_face_force_block_is_still_fail_closed(self) -> None:
        audit = {
            "status": "pass",
            "identifiable_person": True,
            "reason": "fixture",
        }
        result = visual_qa_module._apply_no_face_policy(audit)
        self.assertEqual(result["status"], "block")
        self.assertEqual(result["no_face_policy"], "block")
        self.assertTrue(
            str(result["reason"]).startswith("no_face_policy_identifiable_person")
        )

    def test_canonical_cultural_and_safety_gates_remain_present(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "canonical_visual_evidence_v1.py"
        ).read_text(encoding="utf-8")
        self.assertIn("NO-FACE POLICY", source)
        self.assertIn("CULTURAL & ISLAMIC SUITABILITY GATE", source)
        self.assertIn("advertiser-safe", source)
        self.assertIn("fail closed", source)


if __name__ == "__main__":
    unittest.main()
