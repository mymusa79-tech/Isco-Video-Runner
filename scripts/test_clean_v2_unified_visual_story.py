from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clean_v2 import media as media_module
from clean_v2 import providers as providers_module
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.short_format import TEMPLATE_ORDER
from clean_v2 import visual_qa as visual_qa_module
from clean_v2.visual_qa import _retention_quality_target
from clean_v2.pipeline import (
    _bound_ai_still_preferences,
    _select_longform_narrative_profile,
    _validate_podcast_listener_proxy_script,
    _voice_performance_mode_for_brief,
    _bound_short_visual_story,
    _persist_planning_artifacts,
    _planning_prompt,
    _script_prompt,
    _validate_plan_for_brief,
)
from clean_v2.visual_story import (
    CHANNEL_VISUAL_IDENTITY,
    bind_visual_story_to_script,
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
    section_count = 3 if fmt == "short" else 5
    section_queries = [
        "closed notebook beside unfinished task hands only",
        "phone face down beside one unfinished task hands only",
        "door opening into quiet workspace back view",
        "calendar page with one completed mark hands only",
        "shoes crossing doorway toward morning light no face",
    ]
    section_alts = [
        "two task objects at visibly different starting positions",
        "single object selected from surrounding clutter hands only",
        "workspace cleared except one next-step object no face",
    ]
    sections = [
        {
            "id": f"s{index}",
            "heading": f"قسم {index}",
            "purpose": f"يفهم المشاهد الفكرة {index}",
            "visual_query_en": section_queries[index - 1],
            **(
                {"visual_query_alt_en": section_alts[index - 1]}
                if fmt == "short"
                else {}
            ),
        }
        for index in range(1, section_count + 1)
    ]

    if fmt == "short":
        beat_specs = [
            ("s1", "closed notebook beside unfinished task hands only", "دفتر مغلق بجوار مهمة غير مكتملة"),
            ("s1", "phone scrolling beside unfinished personal task hands only", "هاتف يزاحم المهمة الشخصية غير المكتملة"),
            ("s1", "two progress markers at visibly different starting positions", "نقطتا بداية مختلفتان بوضوح"),
            ("s2", "door opening into quiet workspace back view", "انتقال مرئي إلى مساحة أكثر وضوحًا"),
            ("s2", "hands placing phone face down away from unfinished task", "إبعاد الهاتف عن المهمة غير المكتملة"),
            ("s3", "hand choosing one next step object from surrounding clutter", "اختيار خطوة واحدة من بين المشتتات"),
            ("s3", "single completed progress marker beside next step object", "علامة تقدم مكتملة وخطوة تالية واضحة"),
        ]
    else:
        beat_specs = [
            (f"s{index}", section_queries[index - 1], f"مشهد ملموس يوضح الفكرة {index}")
            for index in range(1, 6)
        ]

    beats = []
    for index, (section_id, query, meaning) in enumerate(beat_specs, start=1):
        is_first = index == 1
        is_last = index == len(beat_specs)
        beats.append(
            {
                "id": f"b{index}",
                "section_id": section_id,
                "viewer_intent": f"يفهم المشاهد التحول {index}",
                "meaning_target": meaning,
                "semantic_must_have": [query],
                "shot_intent": query,
                "role": "hook" if is_first else "payoff" if is_last else "body",
                "stock_query_en": query,
                "display_text_ar": f"لحظة مختلفة {index}",
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
                "visual_motif": "دفتر مغلق يصبح علامة تقدم مكتملة",
            },
            "beats": beats,
        },
    }


class UnifiedVisualStoryPlanningTests(unittest.TestCase):
    def test_hook_stop_power_and_semantic_variety_apply_to_every_format_without_extra_stage(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                prompt = " ".join(_planning_prompt(_brief(fmt)).split())
                self.assertIn("HOOK VISUAL STOP-POWER", prompt)
                self.assertIn("HOOK COVERAGE CONTRACT", prompt)
                self.assertIn("The first body beat must not repeat the hook's dominant scene/action family", prompt)
                self.assertIn("MUST NOT be a calm mood-only establishing image", prompt)
                self.assertIn("understood with sound off in the first frame", prompt)
                self.assertIn("Avoid unrelated shock", prompt)
                self.assertIn("VISUAL VARIETY is semantic, not cosmetic", prompt)
                self.assertIn("Do not place the same dominant action family in consecutive beats", prompt)
                self.assertIn("stuck -> choosing -> moving -> completed", prompt)
                self.assertIn("POST-HOOK VISUAL FLOOR", prompt)
                self.assertIn("Person scrolling many tabs on a laptop", prompt)
                self.assertIn("shot_intent MUST be a concrete English visual description", prompt)
                self.assertIn("specific enough to search directly", prompt)
                self.assertIn("stock_still when a still explains the idea more clearly", prompt)
                self.assertIn("simple chart when directly relevant and readable", prompt)
                self.assertIn("Do not force a still quota", prompt)

    def test_longform_writer_contract_keeps_one_explicit_spoken_cta_aligned_to_visual(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt):
                prompt = " ".join(_script_prompt(_brief(fmt), _planning_value(fmt)).split())
                self.assertIn("FINAL SPOKEN SCRIPT must explicitly contain the exact LOCKED_PLAN.cta once", prompt)
                self.assertIn("voice and visual must appear together", prompt)
                self.assertIn("very next sentence returns naturally to the episode", prompt)

    def test_post_hook_visual_floor_prefers_stronger_alternate_for_every_format(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                value = _planning_value(fmt)
                beat = next(
                    item
                    for item in value["visual_story"]["beats"]
                    if item["section_id"] == "s2"
                )
                beat["shot_intent"] = "person scrolling through many tabs on laptop screen"
                beat["stock_query_en"] = beat["shot_intent"]
                beat["stock_query_alt_en"] = (
                    "hands closing extra browser tabs until one choice remains"
                )
                planned = _validate_plan_for_brief(value, _brief(fmt))
                resolved = next(
                    item
                    for item in planned["visual_story"]["beats"]
                    if item["section_id"] == "s2"
                )
                self.assertIn("closing", resolved["shot_intent"])
                self.assertNotIn("scrolling", resolved["shot_intent"])
                self.assertEqual(
                    planned["_visual_semantic_strength_contract"],
                    "v1_post_hook",
                )

    def test_short_post_hook_visual_floor_can_use_section_level_alternate_locally(self) -> None:
        value = _planning_value("short")
        beat = next(
            item
            for item in value["visual_story"]["beats"]
            if item["section_id"] == "s2"
        )
        beat["shot_intent"] = "person scrolling through many tabs on laptop screen"
        beat["stock_query_en"] = beat["shot_intent"]
        beat.pop("stock_query_alt_en", None)
        planned = _validate_plan_for_brief(value, _brief("short"))
        resolved = next(
            item
            for item in planned["visual_story"]["beats"]
            if item["section_id"] == "s2"
        )
        self.assertIn("selected", resolved["shot_intent"])
        self.assertNotIn("scrolling", resolved["shot_intent"])

    def test_longform_post_hook_visual_floor_fails_closed_without_stronger_alternate(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt):
                value = _planning_value(fmt)
                beat = next(
                    item
                    for item in value["visual_story"]["beats"]
                    if item["section_id"] == "s2"
                )
                beat["shot_intent"] = "person scrolling through many tabs on laptop screen"
                beat["stock_query_en"] = beat["shot_intent"]
                beat.pop("stock_query_alt_en", None)
                with self.assertRaisesRegex(
                    ValueError,
                    "post-hook semantic drop requires a stronger observable alternate",
                ):
                    _validate_plan_for_brief(value, _brief(fmt))

    def test_fresh_plans_enable_fail_closed_visual_diversity_for_every_format(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                planned = _validate_plan_for_brief(_planning_value(fmt), _brief(fmt))
                self.assertEqual(
                    planned["_visual_diversity_contract"],
                    "v2_fail_closed",
                )

    def test_film_writer_binding_rejects_adjacent_stationery_without_alternate(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("film"), _brief("film"))
        visual_story = dict(planned.pop("visual_story"))
        visual_story["beats"][0]["shot_intent"] = "hand writing in notebook"
        visual_story["beats"][0]["stock_query_en"] = "hand writing in notebook"
        visual_story["beats"][1]["shot_intent"] = "pen marking sticky notes on paper"
        visual_story["beats"][1]["stock_query_en"] = "pen marking sticky notes on paper"
        visual_story["beats"][1].pop("stock_query_alt_en", None)
        planned["sections"][1].pop("visual_query_alt_en", None)
        script = {
            "title": "نص نهائي",
            "sections": [
                {
                    "id": section["id"],
                    "narration": f"معنى نهائي مكتمل للقسم {index}. وتظهر نتيجة واضحة.",
                }
                for index, section in enumerate(planned["sections"], start=1)
            ],
        }
        with self.assertRaisesRegex(
            ValueError,
            "repeat the previous stationery scene family",
        ):
            bind_visual_story_to_script(visual_story, planned, script)

    def test_short_visual_story_uses_seven_authored_beats_to_avoid_long_repetition(self) -> None:
        story = {
            "beats": [
                {"id": "b1", "section_id": "s1", "role": "hook", "stock_query_en": "unequal starting marks wide shot"},
                {"id": "b2", "section_id": "s1", "role": "body", "stock_query_en": "different progress positions close detail"},
                {"id": "b3", "section_id": "s1", "role": "body", "stock_query_en": "same path different starting points"},
                {"id": "b4", "section_id": "s2", "role": "body", "stock_query_en": "person checks own progress marker"},
                {"id": "b5", "section_id": "s2", "role": "body", "stock_query_en": "phone comparison feed beside task"},
                {"id": "b6", "section_id": "s3", "role": "body", "stock_query_en": "one chosen next step object"},
                {"id": "b7", "section_id": "s3", "role": "payoff", "stock_query_en": "completed personal progress marker"},
            ]
        }
        bounded = _bound_short_visual_story(story, max_beats=7)
        beats = bounded["beats"]
        self.assertEqual(len(beats), 7)
        self.assertEqual(beats[0]["id"], "b1")
        self.assertEqual(beats[-1]["id"], "b7")
        self.assertEqual(beats[0]["role"], "hook")
        self.assertEqual(beats[-1]["role"], "payoff")
        self.assertEqual({beat["section_id"] for beat in beats}, {"s1", "s2", "s3"})
        self.assertEqual([beat["section_id"] for beat in beats], ["s1", "s1", "s1", "s2", "s2", "s3", "s3"])
        self.assertTrue(all(beat["role"] == "hook" for beat in beats[:3]))

    def test_ai_stills_remain_sparse_inside_existing_scene_budget_for_all_formats(self) -> None:
        base = {
            "beats": [
                {"id": "b1", "role": "hook", "source_preference": "ai_still"},
                {"id": "b2", "role": "body", "source_preference": "ai_still"},
                {"id": "b3", "role": "body", "source_preference": "ai_still"},
                {"id": "b4", "role": "payoff", "source_preference": "ai_still"},
            ]
        }
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                bounded = _bound_ai_still_preferences(base, fmt=fmt)
                beats = bounded["beats"]
                ai = [
                    beat for beat in beats
                    if beat["source_preference"] == "ai_still"
                ]
                self.assertEqual(len(beats), 4)
                self.assertEqual(len(ai), 2)
                self.assertEqual(
                    {beat["role"] for beat in ai},
                    {"hook", "payoff"},
                )

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
                self.assertEqual(len(story["beats"]), 7 if fmt == "short" else 5)
                self.assertTrue(
                    all(
                        beat["source_preference"] in {"stock_motion", "stock_still", "ai_still"}
                        for beat in story["beats"]
                    )
                )

    def test_planning_prompt_forbids_duration_driven_extra_beats(self) -> None:
        for fmt in ("short", "film", "podcast"):
            prompt = " ".join(_planning_prompt(_brief(fmt)).split())
            self.assertIn("Create a new beat ONLY when the idea", prompt)
            self.assertIn("NEVER invent extra beats to hit a", prompt)
            self.assertIn("Hook, body, and payoff all follow the same semantic-quality rule", prompt)
            self.assertIn("one simple concrete visual metaphor", prompt)
            self.assertIn("Do not force a still quota", prompt)
            self.assertIn("simple chart when directly relevant and readable", prompt)
            self.assertIn("normally at most one beat in a Short", prompt)
            self.assertIn("AI images MUST be image-only", prompt)
            self.assertIn("display_text_ar must be a unique natural Arabic phrase", prompt)
            self.assertIn("stock_query_en remains a separate English retrieval fallback", prompt)
            self.assertIn("6-14 useful search words", prompt)
            self.assertIn("hands only, back view, or objects only", prompt)
            if fmt == "short":
                self.assertIn(
                    "payoff_answer must be a descriptive resolution", prompt
                )
                self.assertIn("return EXACTLY 7 semantic visual beats", prompt)

    def test_writer_binds_final_narration_into_visual_story_without_new_stage(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                planned = _validate_plan_for_brief(_planning_value(fmt), _brief(fmt))
                visual_story = dict(planned.pop("visual_story"))
                script = {
                    "title": "نص نهائي",
                    "sections": [
                        {
                            "id": section["id"],
                            "narration": (
                                f"هذا هو المعنى النهائي للقسم {index}. "
                                f"ثم تظهر نتيجة مختلفة مرتبطة بالفكرة {index}."
                            ),
                        }
                        for index, section in enumerate(planned["sections"], start=1)
                    ],
                }
                bound = bind_visual_story_to_script(
                    visual_story,
                    planned,
                    script,
                )
                self.assertTrue(
                    all(beat.get("writer_anchor_ar") for beat in bound["beats"])
                )
                self.assertTrue(
                    all(
                        str(beat["shot_intent"]).isascii()
                        for beat in bound["beats"]
                    )
                )
                self.assertNotIn("warm", bound["beats"][0]["shot_intent"].lower())
                self.assertIn("notebook", bound["beats"][0]["shot_intent"].lower())
                prompt = " ".join(_planning_prompt(_brief(fmt)).split())
                self.assertNotIn(
                    "VISUAL QUALITY CONTRACT (Short, Film, and Podcast)",
                    prompt,
                )
                self.assertIn(
                    '"shot_intent": "6-14 word concrete English observable action/state, directly searchable"',
                    prompt,
                )

    def test_writer_query_compaction_never_leaves_run36_dangling_fragments(self) -> None:
        samples = {
            (
                "person writing in a notebook with a pen then pausing to look "
                "at a messy desk with scattered papers medium shot warm side light"
            ): "person writing in a notebook with a pen",
            (
                "hands sorting through a pile of objects selecting one carefully "
                "then placing it in a clear container close-up soft directional light"
            ): "hands sorting through a pile of objects selecting one carefully",
        }
        for fmt in ("short", "film", "podcast"):
            for raw, expected in samples.items():
                with self.subTest(fmt=fmt, raw=raw):
                    planned = _validate_plan_for_brief(_planning_value(fmt), _brief(fmt))
                    visual_story = dict(planned.pop("visual_story"))
                    visual_story["beats"][0]["shot_intent"] = raw
                    visual_story["beats"][0]["stock_query_en"] = raw
                    script = {
                        "title": "نص نهائي",
                        "sections": [
                            {
                                "id": section["id"],
                                "narration": f"معنى نهائي مكتمل للقسم {index}. وتظهر نتيجة واضحة.",
                            }
                            for index, section in enumerate(planned["sections"], start=1)
                        ],
                    }
                    bound = bind_visual_story_to_script(
                        visual_story,
                        planned,
                        script,
                    )
                    first = bound["beats"][0]
                    self.assertEqual(first["shot_intent"], expected)
                    self.assertEqual(first["stock_query_en"], expected)
                    self.assertLessEqual(len(expected.split()), 14)
                    self.assertNotRegex(expected, r"\b(?:a|at|in|to|with)$")

    def test_screen_semantics_do_not_conflict_with_host_text_safety_for_any_format(self) -> None:
        for fmt in ("short", "film", "podcast"):
            with self.subTest(fmt=fmt):
                planned = _validate_plan_for_brief(_planning_value(fmt), _brief(fmt))
                visual_story = dict(planned.pop("visual_story"))
                first = visual_story["beats"][0]
                first["shot_intent"] = (
                    "hands holding smartphone with conflicting notification shapes on screen"
                )
                first["stock_query_en"] = first["shot_intent"]
                first["semantic_must_have"] = [
                    "smartphone screen with conflicting notification shapes"
                ]
                # This test is about screen/UI safety, while still exercising
                # the real fail-closed diversity contract. Make every later beat a
                # genuinely different active family so unrelated fixture repetition
                # cannot mask the screen-safety assertion.
                distinct_later_scenes = [
                    "hands typing on keyboard beside closed notebook",
                    "back view walking through quiet corridor",
                    "closed door beside empty hallway",
                    "open window above quiet table",
                    "hand writing one line in notebook",
                ]
                for index, beat in enumerate(visual_story["beats"][1:]):
                    scene = distinct_later_scenes[index % len(distinct_later_scenes)]
                    beat["shot_intent"] = scene
                    beat["stock_query_en"] = scene
                    beat["semantic_must_have"] = [scene]
                    beat.pop("stock_query_alt_en", None)
                script = {
                    "title": "نص نهائي",
                    "sections": [
                        {
                            "id": section["id"],
                            "narration": f"معنى نهائي مكتمل للقسم {index}. وتظهر نتيجة واضحة.",
                        }
                        for index, section in enumerate(planned["sections"], start=1)
                    ],
                }
                bound = bind_visual_story_to_script(
                    visual_story,
                    planned,
                    script,
                )
                first_bound = bound["beats"][0]
                avoids = " | ".join(first_bound["semantic_should_avoid"])
                self.assertIn("smartphone screen", first_bound["semantic_must_have"][0])
                self.assertNotIn("logos, UI, or watermarks", avoids)
                self.assertIn("interface copy must stay abstract and illegible", avoids)
                prompt = media_module._ai_still_prompt(
                    bound,
                    first_bound,
                    fmt=fmt,
                    with_reference=False,
                )
                self.assertIn("screen or interface is required", prompt)
                self.assertIn("all copy illegible", prompt)
                self.assertNotIn("text, UI", prompt)
                self.assertNotIn("watermarks, UI", prompt)

    def test_writer_binding_avoids_adjacent_repeated_action_family_without_provider_call(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("short"), _brief("short"))
        visual_story = dict(planned.pop("visual_story"))
        visual_story["beats"][0]["shot_intent"] = "hand writing in notebook at desk"
        visual_story["beats"][1]["shot_intent"] = "person typing notes at keyboard"
        planned["sections"][1]["visual_query_alt_en"] = "person walking through quiet hallway no face"

        script = {
            "title": "نص نهائي",
            "sections": [
                {
                    "id": section["id"],
                    "narration": (
                        f"هذه هي الجملة النهائية للقسم {index}. "
                        f"ثم يتغير المعنى في القسم {index}."
                    ),
                }
                for index, section in enumerate(planned["sections"], start=1)
            ],
        }
        bound = bind_visual_story_to_script(visual_story, planned, script)

        self.assertIn("writing", bound["beats"][0]["shot_intent"])
        # Typing/laptop is intentionally a different family from stationery,
        # so this genuinely different second beat should remain unchanged.
        self.assertIn("typing", bound["beats"][1]["shot_intent"])
        self.assertNotIn("walking", bound["beats"][1]["shot_intent"])
        self.assertEqual(
            bound["beats"][1]["stock_query_en"],
            bound["beats"][1]["shot_intent"],
        )

    def test_writer_binding_closes_real_stationery_repeat_with_existing_alternate(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("short"), _brief("short"))
        visual_story = dict(planned.pop("visual_story"))
        visual_story["beats"][0]["shot_intent"] = (
            "hands frozen above empty notebook with pen and loose paper"
        )
        visual_story["beats"][1]["shot_intent"] = (
            "hands sorting sticky notes and selecting one task"
        )
        visual_story["beats"][1]["semantic_must_have"] = [
            "selecting one task from several options"
        ]
        visual_story["beats"][2]["shot_intent"] = (
            "hands writing first line in notebook with pen on page"
        )
        planned["sections"][0]["visual_query_alt_en"] = (
            "phone screen with one selected task abstract interface"
        )

        script = {
            "title": "نص نهائي",
            "sections": [
                {
                    "id": section["id"],
                    "narration": (
                        f"هذه هي الجملة النهائية للقسم {index}. "
                        f"ثم يتغير المعنى في القسم {index}."
                    ),
                }
                for index, section in enumerate(planned["sections"], start=1)
            ],
        }
        bound = bind_visual_story_to_script(visual_story, planned, script)

        self.assertIn("notebook", bound["beats"][0]["shot_intent"])
        self.assertIn("selected task", bound["beats"][1]["shot_intent"])
        self.assertNotIn("sticky", bound["beats"][1]["shot_intent"])
        # A genuinely different middle scene resets adjacency, so the payoff may
        # intentionally return to the opening motif in a changed state.
        self.assertIn("writing", bound["beats"][2]["shot_intent"])

        hook_context = contextual_intent(
            bound,
            bound["beats"][0]["id"],
            bound["beats"][0]["shot_intent"],
        )
        self.assertIn("Role:hook", hook_context)
        self.assertIn("Meaning:", hook_context)
        self.assertIn("Must show:", hook_context)
        self.assertIn("Current:", hook_context)

    def test_writer_binding_rejects_diversity_alternate_that_changes_beat_meaning(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("short"), _brief("short"))
        visual_story = dict(planned.pop("visual_story"))
        visual_story["beats"][0]["shot_intent"] = (
            "hands frozen above empty notebook with pen and loose paper"
        )
        visual_story["beats"][1]["shot_intent"] = (
            "hands sorting sticky notes and selecting one task"
        )
        visual_story["beats"][1]["semantic_must_have"] = [
            "selecting one task from several options"
        ]
        planned["sections"][0]["visual_query_alt_en"] = (
            "half empty bookshelf with one book pulled out no face"
        )
        script = {
            "title": "نص نهائي",
            "sections": [
                {
                    "id": section["id"],
                    "narration": f"معنى نهائي مكتمل للقسم {index}. وتظهر نتيجة واضحة.",
                }
                for index, section in enumerate(planned["sections"], start=1)
            ],
        }
        with self.assertRaisesRegex(
            ValueError,
            "repeat the previous stationery scene family",
        ):
            bind_visual_story_to_script(visual_story, planned, script)

    def test_stock_result_ranking_uses_existing_metadata_as_semantic_tiebreaker(self) -> None:
        common = {
            "index": 3,
            "count": 12,
            "width": 1080,
            "height": 1920,
            "duration": 6.0,
            "portrait": True,
            "query": "hands frozen above empty notebook pen loose paper",
        }
        matching = media_module._stock_local_rank_score(
            **common,
            metadata="female hands pen over empty notebook paper",
        )
        generic = media_module._stock_local_rank_score(
            **common,
            metadata="sunset ocean travel landscape",
        )
        self.assertGreater(matching, generic)

    def test_writer_overlay_copy_never_becomes_generated_image_text(self) -> None:
        planned = _validate_plan_for_brief(_planning_value("short"), _brief("short"))
        visual_story = dict(planned.pop("visual_story"))
        visual_story["beats"][0]["shot_intent"] = (
            "hand holds card with Arabic text saying start now"
        )
        visual_story["beats"][0]["semantic_must_have"] = [
            "card with readable Arabic text saying start now",
            "hesitating hand",
        ]
        visual_story["beats"][0]["display_text_ar"] = "ابدأ بخطوة"

        script = {
            "title": "نص نهائي",
            "sections": [
                {
                    "id": section["id"],
                    "narration": (
                        f"هذه هي الجملة النهائية للقسم {index}. "
                        f"وهذا هو المعنى الذي يراه المشاهد في القسم {index}."
                    ),
                }
                for index, section in enumerate(planned["sections"], start=1)
            ],
        }
        bound = bind_visual_story_to_script(visual_story, planned, script)
        first = bound["beats"][0]

        self.assertEqual(first["display_text_ar"], "ابدأ بخطوة")
        self.assertNotIn("Arabic text", first["shot_intent"])
        self.assertNotIn("saying start now", first["shot_intent"])
        self.assertTrue(
            all("readable Arabic text" not in item for item in first["semantic_must_have"])
        )
        self.assertTrue(
            any("readable text" in item for item in first["semantic_should_avoid"])
        )
        self.assertTrue(first.get("writer_anchor_ar"))

        visual_story["beats"][0]["shot_intent"] = "Arabic text saying start now"
        rebound = bind_visual_story_to_script(visual_story, planned, script)
        self.assertNotIn("Arabic text", rebound["beats"][0]["shot_intent"])
        self.assertNotIn("saying start now", rebound["beats"][0]["shot_intent"])

    def test_channel_visual_world_is_grounded_deep_and_progress_oriented_for_all_formats(self) -> None:
        self.assertIn("quiet premium depth", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("dark navy and charcoal", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("earned small wins", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("glossy lifestyle brightness", CHANNEL_VISUAL_IDENTITY)
        for fmt in ("short", "film", "podcast"):
            prompt = " ".join(_planning_prompt(_brief(fmt)).split())
            self.assertIn("grounded upward movement", prompt)
            self.assertIn("moderate-to-deep exposure", prompt)
            self.assertIn("glossy, airy lifestyle-ad bright", prompt)
            self.assertIn("generic coffee/laptop mood shots", prompt)

    def test_supported_editorial_shape_counts_stay_exact(self) -> None:
        self.assertEqual(
            LONGFORM_NARRATIVE_FORMATS,
            frozenset({
                "direct_cinematic",
                "question_answer",
                "dialogue_qa",
                "inner_dialogue",
                "problem_reveal_solution",
                "story_analysis",
                "paradox",
                "hypothesis_test",
                "connected_list",
            }),
        )
        self.assertEqual(
            TEMPLATE_ORDER,
            (
                "why_reframe",
                "inner_dialogue",
                "micro_story",
                "quote_reflection",
            ),
        )

    def test_longform_narrative_profiles_are_deterministic_and_topic_fit(self) -> None:
        cases = {
            "لماذا تفشل خطط إدارة الوقت في الحياة اليومية؟": "problem_reveal_solution",
            "قصة رجل بدأ من جديد بعد سنوات من التردد": "story_analysis",
            "هل فعلًا الانتظار يزيد الدافع؟": "hypothesis_test",
            "مفارقة الراحة: لماذا كلما ارتحت أكثر شعرت بالخمول؟": "paradox",
            "5 أسباب تجعل البداية أصعب مما تبدو": "connected_list",
            "حوار حول الاعتراض على فكرة الانضباط": "dialogue_qa",
            "أقول لنفسي إنني بدأت أفوز أخيرًا": "inner_dialogue",
        }
        for topic, expected in cases.items():
            brief = _brief("film")
            brief["approved_topic"] = topic
            first = _select_longform_narrative_profile(brief)
            second = _select_longform_narrative_profile(brief)
            self.assertEqual(first, second)
            self.assertEqual(first["narrative_format"], expected)
            self.assertEqual(first["extra_ai_calls"], 0)
            self.assertTrue(first["writing"])
            self.assertTrue(first["visual"])
            self.assertTrue(first["voice"])

    def test_podcast_listener_proxy_validator_keeps_orus_sparse_and_charon_primary(self) -> None:
        script = {
            "sections": [
                {
                    "id": "s1",
                    "narration": (
                        "A: لماذا أعرف ما يجب فعله ومع ذلك لا أبدأ؟ "
                        "B: لأن معرفة الخطوة لا تعني أن الاحتكاك اختفى. "
                        "حين تبدو البداية أكبر من طاقتك، يتأخر الفعل حتى لو كان الهدف واضحًا."
                    ),
                },
                {
                    "id": "s2",
                    "narration": (
                        "A: إذًا المشكلة ليست أنني لا أريد التغيير؟ "
                        "B: ليس بالضرورة. أحيانًا تحتاج أن تجعل أول حركة أوضح وأصغر، ثم تترك النتيجة تخبرك إن كان الاتجاه مناسبًا."
                    ),
                },
            ]
        }
        report = _validate_podcast_listener_proxy_script(script)
        self.assertEqual(report["mode"], "listener_proxy_qa")
        self.assertEqual(report["first_speaker"], "A")
        self.assertEqual(report["voices"], {"A": "Orus", "B": "Charon"})
        self.assertLessEqual(report["questioner_share"], 0.35)

    def test_podcast_is_fixed_listener_proxy_dialogue_house_style(self) -> None:
        for topic in (
            "لماذا نشعر أننا متأخرون؟",
            "قصة عن العودة بعد الفشل",
            "مفارقة الراحة والانضباط",
        ):
            brief = _brief("podcast")
            brief["approved_topic"] = topic
            profile = _select_longform_narrative_profile(brief)
            self.assertEqual(profile["narrative_format"], "dialogue_qa")
            self.assertEqual(profile["voice"], "podcast_listener_proxy_qa")
            self.assertEqual(profile["selection_basis"], "podcast_fixed_house_style")
            self.assertIn("listener", profile["writing"])
            self.assertIn("A is never a host", profile["writing"])
            prompt = _planning_prompt(brief)
            self.assertIn("LOCKED NARRATIVE PROFILE", prompt)
            self.assertIn("narrative_format=dialogue_qa", prompt)
            self.assertIn("A listener-proxy turn does NOT force a scene cut", prompt)
            self.assertIn("Never fake two hosts", prompt)

    def test_writer_and_voice_use_the_same_locked_narrative_profile(self) -> None:
        film = _brief("film")
        film["approved_topic"] = "لماذا تفشل خطط إدارة الوقت في الحياة اليومية؟"
        planned = _validate_plan_for_brief(_planning_value("film"), film)
        story = planned.pop("visual_story")
        self.assertEqual(planned["narrative_format"], "problem_reveal_solution")
        prompt = _script_prompt(film, planned, visual_story=story)
        self.assertIn("LOCKED NARRATIVE PERFORMANCE PROFILE", prompt)
        self.assertIn("narrative_format=problem_reveal_solution", prompt)
        self.assertEqual(
            _voice_performance_mode_for_brief(film, planned),
            "problem_reveal_solution",
        )

        podcast = _brief("podcast")
        planned_podcast = _validate_plan_for_brief(_planning_value("podcast"), podcast)
        story_podcast = planned_podcast.pop("visual_story")
        self.assertEqual(planned_podcast["narrative_format"], "dialogue_qa")
        podcast_prompt = _script_prompt(
            podcast,
            planned_podcast,
            visual_story=story_podcast,
        )
        self.assertIn("A maps to Orus", podcast_prompt)
        self.assertIn("B maps to Charon", podcast_prompt)
        self.assertIn("Use A sparingly", podcast_prompt)
        self.assertIn("A is sparse and short", podcast_prompt)
        self.assertEqual(
            _voice_performance_mode_for_brief(podcast, planned_podcast),
            "podcast_listener_proxy_qa",
        )

    def test_each_format_has_distinct_visual_grammar_inside_one_channel_identity(self) -> None:
        short_prompt = " ".join(_planning_prompt(_brief("short")).split())
        film_prompt = " ".join(_planning_prompt(_brief("film")).split())
        podcast_prompt = " ".join(_planning_prompt(_brief("podcast")).split())

        for prompt in (short_prompt, film_prompt, podcast_prompt):
            self.assertIn("Use quiet premium darkness rather than gloom", prompt)
            self.assertIn("avoid flat beige/washed-out warm-neutral stock", prompt)
            self.assertIn("warm gold only as a restrained accent", prompt)

        self.assertIn("FORMAT VISUAL PROFILE — SHORT", short_prompt)
        self.assertIn("quicker visible state changes", short_prompt)
        self.assertIn("FORMAT VISUAL PROFILE — FILM", film_prompt)
        self.assertIn("wider lived-in environments", film_prompt)
        self.assertIn("FORMAT VISUAL PROFILE — PODCAST", podcast_prompt)
        self.assertIn("thoughtful room around the voice", podcast_prompt)
        self.assertNotIn("FORMAT VISUAL PROFILE — PODCAST", short_prompt)
        self.assertNotIn("FORMAT VISUAL PROFILE — SHORT", podcast_prompt)

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

    def test_mistral_planning_schema_requires_visual_story_but_legacy_validator_can_fallback(self) -> None:
        schema = providers_module._mistral_planning_response_schema(
            _planning_prompt(_brief("film"))
        )
        self.assertIn("visual_story", schema["required"])
        self.assertIn("visual_story", schema["properties"])
        beats = schema["properties"]["visual_story"]["properties"]["beats"]
        self.assertEqual(beats["minItems"], 5)
        self.assertEqual(beats["maxItems"], 15)

        compact = _planning_value("film")
        compact.pop("visual_story")
        planned = _validate_plan_for_brief(compact, _brief("film"))
        story = planned["visual_story"]
        self.assertTrue(story["beats"])
        self.assertEqual(story["beats"][0]["role"], "hook")
        self.assertEqual(story["beats"][-1]["role"], "payoff")
        self.assertEqual(
            {beat["section_id"] for beat in story["beats"]},
            {section["id"] for section in planned["sections"]},
        )
        self.assertEqual(story["beats"][0]["source_preference"], "ai_still")
        self.assertEqual(story["beats"][-1]["source_preference"], "ai_still")

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
        self.assertIn("LOCKED_PLAN.practical_action_ar is already final and host-owned", prompt)
        self.assertIn("under s3_payoff instead of narration", prompt)
        self.assertIn("s3_locked_action, validates both fields separately", prompt)
        self.assertNotIn("express payoff_answer as descriptive resolution", prompt)
        self.assertNotIn("Do not optimize for a fixed word count or duration", prompt)

    def test_fresh_story_repairs_repeated_search_but_rejects_repeated_intent(self) -> None:
        planned = _planning_value("short")
        story = planned["visual_story"]
        story["beats"][1]["stock_query_en"] = story["beats"][0]["stock_query_en"]
        validated = validate_visual_story(story, planned)
        self.assertEqual(
            validated["beats"][1]["stock_query_en"],
            planned["sections"][0]["visual_query_alt_en"],
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
            ["hook", "body", "body", "body", "body", "body", "payoff"],
        )

    def test_fresh_story_preserves_semantic_source_choice_across_all_roles(self) -> None:
        planned = _planning_value("short")
        planned["visual_story"]["beats"][0]["source_preference"] = "stock_motion"
        planned["visual_story"]["beats"][1]["source_preference"] = "ai_still"
        planned["visual_story"]["beats"][-1]["source_preference"] = "stock_motion"
        validated = validate_visual_story(planned["visual_story"], planned)
        self.assertEqual(
            [beat["source_preference"] for beat in validated["beats"]],
            [
                "stock_motion",
                "ai_still",
                "stock_motion",
                "stock_motion",
                "stock_motion",
                "stock_motion",
                "stock_motion",
            ],
        )
        self.assertEqual(
            [beat["display_text_ar"] for beat in validated["beats"]],
            [
                "لحظة مختلفة 1",
                "لحظة مختلفة 2",
                "لحظة مختلفة 3",
                "لحظة مختلفة 4",
                "لحظة مختلفة 5",
                "لحظة مختلفة 6",
                "لحظة مختلفة 7",
            ],
        )

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
        self.assertEqual(
            [beat["source_preference"] for beat in validated["beats"]],
            ["ai_still", "ai_still"],
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
            0.92,
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
        self.assertIn("NO-CLEAR-FACE POLICY", source)
        self.assertIn("identifiable_person=true", source)
        self.assertIn("CULTURAL & ISLAMIC SUITABILITY GATE", source)
        self.assertIn("advertiser-safe", source)
        self.assertIn("fail closed", source)


if __name__ == "__main__":
    unittest.main()
