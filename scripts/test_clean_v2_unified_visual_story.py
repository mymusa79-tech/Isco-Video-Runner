from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import providers as providers_module
from clean_v2 import visual_qa as visual_qa_module
from clean_v2.visual_qa import _retention_quality_target
from clean_v2.media import (
    _choose_diverse_stock_query,
    _semantic_query_overlap,
    _visual_action_family,
)
from clean_v2.pipeline import (
    _persist_planning_artifacts,
    _planning_prompt,
    _script_prompt,
    _validate_plan_for_brief,
)
from clean_v2.visual_story import (
    contextual_intent,
    fallback_visual_story,
    validate_visual_story,
)


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
    beats = []
    for index in range(1, count + 1):
        is_first = index == 1
        is_last = index == count
        beats.append(
            {
                "id": f"b{index}",
                "section_id": f"s{index}",
                "viewer_intent": f"يفهم المشاهد التحول {index}",
                "shot_intent": f"دفتر واحد يتغير بصريًا في المرحلة {index}",
                "role": "hook" if is_first else "payoff" if is_last else "body",
                "stock_query_en": f"warm notebook workspace distinct action {index} hands only",
                "source_preference": (
                    "ai_still" if is_first or is_last else "stock_motion"
                ),
            }
        )
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
            "retention_thread": {
                "hook_tension": "لماذا تبقى البداية عالقة رغم وضوح الهدف؟",
                "payoff_answer": "تصغير الفعل الأول يزيل الاحتكاك ويبدأ الحركة.",
                "visual_motif": "دفتر مغلق يصبح صفحة عليها خطوة واحدة مكتملة",
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
            planned["visual_story"]["beats"][0]["source_preference"],
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
            {
                "schema_version",
                "visual_world",
                "story_arc",
                "retention_thread",
                "beats",
            },
        )
        self.assertEqual(disk_story["schema_version"], 2)
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

    def test_same_visual_story_contract_is_valid_for_each_format(self) -> None:
        for fmt in ("short", "film", "podcast"):
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
            self.assertIn("at most THREE AI stills", prompt)
            self.assertIn("stock_query_en is a separate, distinct", prompt)
            self.assertIn("6-14 useful search words", prompt)
            self.assertIn("hands only, back view, or objects only", prompt)
            if fmt == "short":
                self.assertIn(
                    "payoff_answer must be a descriptive resolution", prompt
                )

    def test_planning_prompt_requires_semantic_visual_variety_not_prop_swaps(self) -> None:
        prompt = " ".join(_planning_prompt(_brief("short")).split())
        self.assertIn("Visual variety must be SEMANTIC, not cosmetic", prompt)
        self.assertIn(
            "notebook, journal, pen, sticky notes, checklist, and writing as one visual-action family",
            prompt,
        )
        self.assertIn("Do not place the same dominant action/object family in consecutive beats", prompt)
        self.assertIn("stuck -> choosing -> moving -> completing", prompt)

    def test_local_visual_family_guard_treats_writing_props_as_one_family(self) -> None:
        self.assertEqual(_visual_action_family("hands writing in notebook warm light"), "writing")
        self.assertEqual(_visual_action_family("journal with pen and sticky notes closeup"), "writing")
        self.assertEqual(_visual_action_family("typing on laptop keyboard"), "screen")
        selected, family, rewritten = _choose_diverse_stock_query(
            "hands writing in notebook warm light",
            [
                "person walking along quiet path back view",
                "hands typing on laptop keyboard",
            ],
            previous_family="writing",
            family_counts={"writing": 1},
        )
        self.assertEqual(family, "walking")
        self.assertTrue(rewritten)
        self.assertIn("walking", selected)

    def test_stock_metadata_overlap_rewards_exact_meaning_inside_same_result_page(self) -> None:
        query = "person walking quiet path morning"
        relevant = _semantic_query_overlap(query, "walking person on quiet morning path")
        generic = _semantic_query_overlap(query, "coffee notebook desk aesthetic")
        self.assertGreater(relevant, generic)

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
            ["visual_world", "story_arc", "retention_thread", "beats"],
        )
        beat = story["properties"]["beats"]["items"]
        self.assertIn("stock_query_en", beat["required"])
        self.assertEqual(beat["properties"]["role"]["enum"], ["hook", "body", "payoff"])
        self.assertNotIn("progression", beat["properties"])
        self.assertNotIn("hook_relation", beat["properties"])
        self.assertEqual(
            beat["properties"]["source_preference"]["enum"],
            ["stock_motion", "ai_still"],
        )

    def test_script_writer_receives_the_locked_hook_to_payoff_thread(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("film"), _brief("film"))
        story = planned.pop("visual_story")
        prompt = _script_prompt(_brief("film"), planned, visual_story=story)
        self.assertIn("LOCKED_VISUAL_STORY:", prompt)
        self.assertIn("لماذا تبقى البداية عالقة", prompt)
        self.assertIn("تصغير الفعل الأول يزيل الاحتكاك", prompt)
        self.assertIn("every section must advance its beat viewer_intent", prompt)

    def test_short_script_prompt_has_no_hook_length_or_payoff_conflict(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("short"), _brief("short"))
        story = planned.pop("visual_story")
        prompt = _script_prompt(_brief("short"), planned, visual_story=story)
        self.assertIn("hard maximum of 18 Arabic words", prompt)
        self.assertIn("express payoff_answer as descriptive resolution", prompt)
        self.assertNotIn("Do not optimize for a fixed word count or duration", prompt)

    def test_fresh_story_repairs_repeated_search_but_rejects_repeated_intent(self) -> None:
        planned = _planning_value("short")
        story = planned["visual_story"]
        story["beats"][1]["stock_query_en"] = story["beats"][0]["stock_query_en"]
        validated = validate_visual_story(story, planned)
        self.assertEqual(
            validated["beats"][1]["stock_query_en"],
            planned["sections"][1]["visual_query_en"],
        )

        planned = _planning_value("short")
        story = planned["visual_story"]
        story["beats"][1]["viewer_intent"] = story["beats"][0]["viewer_intent"]
        with self.assertRaisesRegex(ValueError, "add new information"):
            validate_visual_story(story, planned)

    def test_fresh_story_normalizes_hook_body_payoff_roles_locally(self) -> None:
        planned = _planning_value("short")
        planned["visual_story"]["beats"][0]["role"] = "body"
        planned["visual_story"]["beats"][-1]["role"] = "body"
        validated = validate_visual_story(planned["visual_story"], planned)
        self.assertEqual(
            [beat["role"] for beat in validated["beats"]],
            ["hook", "body", "payoff"],
        )

    def test_fresh_story_keeps_ai_bookends_and_allows_one_semantic_gap_ai_body(self) -> None:
        planned = _planning_value("short")
        planned["visual_story"]["beats"][0]["source_preference"] = "stock_motion"
        planned["visual_story"]["beats"][1]["source_preference"] = "ai_still"
        planned["visual_story"]["beats"][-1]["source_preference"] = "stock_motion"
        validated = validate_visual_story(planned["visual_story"], planned)
        self.assertEqual(
            [beat["source_preference"] for beat in validated["beats"]],
            ["ai_still", "ai_still", "ai_still"],
        )

        planned = _planning_value("film")
        planned["visual_story"]["beats"][1]["source_preference"] = "ai_still"
        planned["visual_story"]["beats"][2]["source_preference"] = "ai_still"
        with self.assertRaisesRegex(ValueError, "at most one semantic-gap AI"):
            validate_visual_story(planned["visual_story"], planned)

    def test_partial_retention_thread_uses_existing_plan_without_provider_retry(self) -> None:
        planned = _planning_value("podcast")
        planned["visual_story"]["retention_thread"] = {
            "hook_tension": "سؤال حقيقي من الحلقة",
            "payoff_answer": "",
        }
        validated = validate_visual_story(planned["visual_story"], planned)
        self.assertEqual(
            validated["retention_thread"]["hook_tension"],
            "سؤال حقيقي من الحلقة",
        )
        self.assertEqual(
            validated["retention_thread"]["payoff_answer"],
            planned["sections"][-1]["purpose"],
        )
        self.assertEqual(
            validated["retention_thread"]["visual_motif"],
            planned["sections"][0]["visual_query_en"],
        )

    def test_single_section_fallback_still_has_distinct_hook_and_payoff(self) -> None:
        plan = {
            "promise": "فهم خطوة البداية",
            "sections": [
                {
                    "id": "s1",
                    "purpose": "تحويل النية إلى خطوة مرئية",
                    "visual_query_en": "closed notebook beside warm window hands only",
                }
            ],
        }
        story = fallback_visual_story(plan)
        validated = validate_visual_story(story, plan)
        self.assertEqual(len(validated["beats"]), 2)
        self.assertEqual(validated["beats"][0]["role"], "hook")
        self.assertEqual(validated["beats"][-1]["role"], "payoff")
        self.assertTrue(
            all(beat["source_preference"] == "ai_still" for beat in validated["beats"])
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
        self.assertIn("Same hook-to-payoff arc", intent)
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
        self.assertIn("Same hook-to-payoff arc:", intent)
        self.assertTrue(intent.endswith("judge continuity."))
        self.assertLessEqual(len(intent), 300)


class VisualSafetyRegressionTests(unittest.TestCase):
    def test_later_visuals_must_stay_close_to_hook_quality(self) -> None:
        self.assertAlmostEqual(
            _retention_quality_target(hook_floor=0.97, absolute_floor=0.85),
            0.89,
        )
        self.assertEqual(
            _retention_quality_target(hook_floor=0.90, absolute_floor=0.85),
            0.85,
        )

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
