from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import pipeline, providers, short_format, visual_qa
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY, VisualFamilyRepeatError
from scripts.test_clean_v2_unified_visual_story import _brief, _planning_value


LONG_QUERY = "hands arranging unfinished pieces of household repair equipment on a crowded wooden workbench"
GOOD_QUERY = "hands arranging unfinished tools on crowded workbench"


def _family_error() -> VisualFamilyRepeatError:
    return VisualFamilyRepeatError(
        "repeated stationery family", family="stationery", beat_id="b2",
        section_id="s1", query="cluttered desk with crossed out checklist papers",
    )


def _podcast_fixture():
    plan = _planning_value("short")
    plan["cta"] = "ما المقياس الذي ستعيد التفكير فيه؟"
    plan["narrative_format"] = "dialogue_qa"
    plan["_visual_diversity_contract"] = "v2_fail_closed"
    for section in plan["sections"]:
        section.pop("visual_query_alt_en", None)
    story = plan.pop("visual_story")
    story["visual_world"] = CHANNEL_VISUAL_IDENTITY
    queries = [
        "hands crossing out checklist items with red pen",
        "cluttered desk with many crossed out checklist papers",
        "empty bookshelf with one highlighted book",
        "hands turning pages of a journal",
        "hands placing completed task card into wooden box",
    ]
    for beat, query in zip(story["beats"], queries):
        beat["shot_intent"] = query
        beat["stock_query_en"] = query
        beat["semantic_must_have"] = [query]
    story["beats"][1]["meaning_target"] = "show overwhelming unfinished tasks as evidence of impossible standards"
    script = {"title": "مقياس التقدم", "sections": [
        {"id": "s1", "narration": "A: لماذا لا نرى تقدمنا؟ B: المهام كثيرة أمامنا. نواصل العمل دون نهاية. ثم نفهم المقياس."},
        {"id": "s2", "narration": "B: حين يرتفع المقياس باستمرار، يصبح الإنجاز أصغر في أعيننا."},
        {"id": "s3", "narration": "B: يمكن أن يكون الإنجاز الصغير دليلًا واضحًا على تقدمنا."},
    ]}
    return _brief("podcast"), plan, story, script


class VisualRecoveryContractTests(unittest.TestCase):
    def _router(self, replies):
        calls = []
        values = iter(replies)

        def invoke(prompt, tokens):
            calls.append((prompt, tokens))
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return {"alternate_query": value}

        router = providers.ProviderRouter((
            providers.ProviderAdapter("gemini_flash_lite", invoke),
            providers.ProviderAdapter("mistral", lambda *_: self.fail("must retain the successful writer")),
        ))
        router.events.append({"stage": "script", "provider": "gemini_flash_lite", "result": "success"})
        return router, calls

    def test_validator_preserves_limits_and_exposes_specific_reason(self):
        with self.assertRaises(visual_qa.AlternateQueryError) as raised:
            visual_qa._validate_alternate_query({"alternate_query": LONG_QUERY}, original_query="paper")
        self.assertEqual(raised.exception.code, "alternate_query_too_long")
        self.assertIn("maximum is 80", str(raised.exception))
        self.assertEqual(visual_qa._validate_alternate_query(
            {"alternate_query": GOOD_QUERY}, original_query="paper"
        )["alternate_query"], GOOD_QUERY)
        for value, code in [
            ({}, "alternate_query_empty"),
            ([], "alternate_query_invalid_shape"),
            ({"alternate_query": "repair tools"}, "alternate_query_word_count"),
            ({"alternate_query": GOOD_QUERY}, "alternate_query_unchanged"),
        ]:
            with self.subTest(code=code), self.assertRaises(visual_qa.AlternateQueryError) as rejected:
                visual_qa._validate_alternate_query(value, original_query=GOOD_QUERY)
            self.assertEqual(rejected.exception.code, code)

    def test_both_recovery_prompts_advertise_existing_character_limit(self):
        _, _, story, _ = _podcast_fixture()
        family = pipeline._visual_family_recovery_prompt(error=_family_error(), visual_story=story)
        final_qa = visual_qa._alternate_visual_query_prompt(original_query="paper", narration_context="tasks")
        self.assertIn("at most 80 characters including spaces", family)
        self.assertIn("at most 80 characters including spaces", final_qa)
        self.assertIn("context, not mandatory props", family)
        self.assertIn("Preserve the exact beat meaning", family)

    def test_final_visual_recovery_prompt_uses_observed_failure_class(self):
        prompt = visual_qa._alternate_visual_query_prompt(
            original_query="office worker close portrait",
            narration_context="ضغط العمل يتراكم",
            semantic_brief="show overload without a recognizable face",
            failure_reason="identifiable_face",
        )
        self.assertIn("Observed failure class", prompt)
        self.assertIn("identifiable_face", prompt)
        self.assertIn("hands/back view/objects/distant framing", prompt)

    def test_visual_recovery_reason_is_local_and_deterministic(self):
        self.assertEqual(
            visual_qa._visual_recovery_reason(
                {"no_face_policy": "block"}, floor=0.95, target=0.80
            ),
            "identifiable_face",
        )
        self.assertEqual(
            visual_qa._visual_recovery_reason(
                {"cultural_islamic_policy": "block"}, floor=0.95, target=0.80
            ),
            "cultural_conflict",
        )
        self.assertEqual(
            visual_qa._visual_recovery_reason(
                {"ai_image_only_policy": "block"}, floor=0.95, target=0.80
            ),
            "embedded_text_or_logo",
        )
        self.assertEqual(
            visual_qa._visual_recovery_reason({}, floor=0.70, target=0.80),
            "weak_semantic_fit",
        )

    def test_second_attempt_receives_rejection_and_only_valid_output_changes_proof(self):
        brief, plan, story, script = _podcast_fixture()
        original = copy.deepcopy(story)
        router, calls = self._router([LONG_QUERY, GOOD_QUERY])
        seen = []

        def bind(**kwargs):
            candidate = kwargs["visual_story"]
            seen.append(copy.deepcopy(candidate))
            if candidate["beats"][1]["stock_query_en"] != GOOD_QUERY:
                raise _family_error()
            return candidate

        with tempfile.TemporaryDirectory() as root, mock.patch.object(pipeline, "_bind_writer_visual_story", side_effect=bind):
            bound = pipeline._bind_writer_visual_story_with_recovery(
                router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual({call[1] for call in calls}, {180})
        self.assertIn("alternate_query_too_long", calls[1][0])
        self.assertIn("maximum is 80", calls[1][0])
        self.assertNotEqual(calls[0][0], calls[1][0])
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(story, original)
        self.assertEqual(bound["beats"][1]["semantic_must_have"], [GOOD_QUERY])
        self.assertEqual(bound["beats"][1]["meaning_target"], original["beats"][1]["meaning_target"])
        failures = [e for e in router.events if e.get("result") == "invalid_output"]
        self.assertTrue(failures[0]["reason"].startswith("invalid_output_valueerror_"))
        self.assertIn("alternate_query_too_long", failures[0]["reason"])
        detail = json.loads(failures[0]["detail"])
        self.assertEqual(detail["alternate_query_chars"], len(LONG_QUERY))
        self.assertEqual(detail["alternate_query_words"], len(LONG_QUERY.split()))
        self.assertNotIn(LONG_QUERY, json.dumps(router.events))

    def test_two_bad_outputs_remain_fail_closed_without_more_calls(self):
        brief, plan, story, script = _podcast_fixture()
        router, calls = self._router([LONG_QUERY, LONG_QUERY])
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "_bind_writer_visual_story", side_effect=_family_error()
        ):
            with self.assertRaises(VisualFamilyRepeatError) as raised:
                pipeline._bind_writer_visual_story_with_recovery(
                    router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
                )
        self.assertIsInstance(raised.exception.__cause__, visual_qa.AlternateQueryError)
        self.assertEqual(len(calls), 2)

    def test_same_family_is_rejected_instead_of_silently_accepted(self):
        brief, plan, story, script = _podcast_fixture()
        router, calls = self._router(["hands writing another task on paper", "hands marking checklist with red pen"])
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "_bind_writer_visual_story", side_effect=_family_error()
        ):
            with self.assertRaises(VisualFamilyRepeatError):
                pipeline._bind_writer_visual_story_with_recovery(
                    router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
                )
        self.assertEqual(len(calls), 2)
        self.assertIn("alternate_query_repeats_rejected_family", calls[1][0])

    def test_provider_outage_retains_original_family_failure_and_call_bound(self):
        brief, plan, story, script = _podcast_fixture()
        outage = providers.ProviderWireFailure("http_503", http_status=503)
        router, calls = self._router([outage, outage])
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "_bind_writer_visual_story", side_effect=_family_error()
        ):
            with self.assertRaises(VisualFamilyRepeatError) as raised:
                pipeline._bind_writer_visual_story_with_recovery(
                    router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
                )
        self.assertIs(raised.exception.__cause__, outage)
        self.assertEqual(len(calls), 2)
        self.assertEqual([e["reason"] for e in router.events if e.get("result") == "failed"], ["http_503", "http_503"])

    def test_run78_scene_pattern_binds_with_one_valid_same_writer_recovery(self):
        brief, plan, story, script = _podcast_fixture()
        original = copy.deepcopy(story)
        router, calls = self._router([GOOD_QUERY])
        with tempfile.TemporaryDirectory() as root:
            bound = pipeline._bind_writer_visual_story_with_recovery(
                router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
            )
            saved = json.loads((Path(root) / "visual-story.json").read_text())
        self.assertEqual(len(calls), 1)
        self.assertEqual(saved, bound)
        self.assertEqual(bound["beats"][1]["semantic_must_have"], [GOOD_QUERY])
        self.assertEqual(bound["beats"][1]["meaning_target"], original["beats"][1]["meaning_target"])
        self.assertEqual(bound["beats"][4]["semantic_must_have"], original["beats"][4]["semantic_must_have"])
        self.assertEqual(story, original)

    def test_two_distinct_rejected_beats_share_the_existing_two_call_budget(self):
        brief, plan, story, script = _podcast_fixture()
        story["beats"][4]["shot_intent"] = "hands placing completed paper task card into wooden box"
        story["beats"][4]["stock_query_en"] = story["beats"][4]["shot_intent"]
        replies = [GOOD_QUERY, "hands removing tangled cords from finished repair tools"]
        router, calls = self._router(replies)
        with tempfile.TemporaryDirectory() as root:
            bound = pipeline._bind_writer_visual_story_with_recovery(
                router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
            )
        self.assertEqual(len(calls), 2)
        self.assertEqual(bound["beats"][4]["semantic_must_have"], [replies[1]])

    def test_authored_alternates_update_proof_without_any_provider_call(self):
        brief, plan, story, script = _podcast_fixture()
        story["beats"][1]["stock_query_alt_en"] = GOOD_QUERY
        story["beats"][4]["stock_query_alt_en"] = "hands removing tangled cords from finished repair tools"
        router, calls = self._router([])
        with tempfile.TemporaryDirectory() as root:
            bound = pipeline._bind_writer_visual_story_with_recovery(
                router=router, output_dir=Path(root), brief=brief, plan=plan, script=script, visual_story=story
            )
        self.assertEqual(calls, [])
        self.assertEqual(bound["beats"][1]["semantic_must_have"], [GOOD_QUERY])


class PlanningCapacityContractTests(unittest.TestCase):
    def test_all_short_templates_fit_groq_with_identity_and_rules_preserved(self):
        brief = _brief("short")
        brief.update({
            "approved_topic": "كيف تبني درعاً صحياً لحماية طاقتك النفسية؟",
            "editorial_intent": "محتوى عربي فصيح طبيعي، متفائل وواقعي، واضح ومفيد، مع تجنب المبالغة والادعاءات غير المدعومة.",
            "hard_constraints": [
                "No fabricated facts.",
                "Use research_pack only within each source claim_scope.",
                "Gemini 3.8 is the only voice provider: Charon is the primary narrator; Orus is allowed only when Planning selects dialogue_qa.",
            ],
            "research_pack": [
                {
                    "claim_scope": "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا.",
                    "source_title": "اتقن فن التجاهل وعدم الاهتمام.. تجاهل الجميع وستندهش كيف يريدك الناس (كتاب صوتي)",
                    "source_url": "https://youtu.be/da9274BCTTA",
                },
                {
                    "claim_scope": "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا.",
                    "source_title": "فخ المجاملة: لماذا يدمر الخجل من الرفض سلامك الداخلي ويفتح باب الاستغلال؟",
                    "source_url": "https://youtu.be/3INesZwo9uY",
                },
                {
                    "claim_scope": "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا.",
                    "source_title": "عادات يومية تزيد من هيبتك | كيف تفرض احترامك دون أن تطلبه",
                    "source_url": "https://youtu.be/nlh9Qu_uw7E",
                },
            ],
        })
        original = copy.deepcopy(brief)
        selection = short_format.select_short_template(brief)
        for template in short_format.TEMPLATE_ORDER:
            selected = dict(selection, template=template,
                            beat_shape=short_format.TEMPLATE_COMPENSATION[template]["beat_shape"],
                            writing_directive=short_format.TEMPLATE_WRITING_DIRECTIVES[template],
                            visual_query_directive=short_format.TEMPLATE_VISUAL_QUERY_DIRECTIVES[template])
            with self.subTest(template=template), mock.patch.object(short_format, "select_short_template", return_value=selected):
                prompt = pipeline._planning_prompt(brief)
                self.assertLess(
                    len(prompt.encode("utf-8")),
                    providers.GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES,
                )
                for marker in ("APPROVED_BRIEF:", "SHORT_FORMAT_CONTRACT:", "ACTION-SPECIFICITY SELF-CHECK",
                               "HOOK SPECIFICITY SELF-CHECK", "VISUAL EVIDENCE CONTRACT", "<CHANNEL_PERSONA>", "<HUMAN_FEEL>"):
                    self.assertIn(marker, prompt)
                self.assertEqual(providers._mistral_planning_response_schema(prompt)["properties"]["sections"]["minItems"], 3)
        self.assertEqual(brief, original)

    def test_film_prompt_fits_groq_planning_stage_limit(self):
        brief = _brief("film")
        brief.update({
            "approved_topic": "لماذا نؤجل التغيير حتى نشعر أننا مستعدون تمامًا؟",
            "editorial_intent": "محتوى عربي فصيح طبيعي، متفائل وواقعي، واضح ومفيد، مع تجنب المبالغة والادعاءات غير المدعومة.",
            "hard_constraints": [
                "No fabricated facts.",
                "Use research_pack only within each source claim_scope.",
                "Gemini 3.8 is the only voice provider: Charon is the primary narrator; Orus is allowed only when Planning selects dialogue_qa.",
            ],
            "research_pack": [
                {
                    "claim_scope": "دليل سوقي على الاهتمام بالموضوع فقط؛ لا يثبت سببية أو تشخيصًا.",
                    "source_title": "لماذا ننتظر اللحظة المناسبة قبل أن نبدأ؟",
                    "source_url": "https://youtu.be/example",
                }
            ],
        })
        prompt = pipeline._planning_prompt(brief)
        self.assertLess(
            len(prompt.encode("utf-8")),
            providers.GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES,
        )
        for marker in (
            "APPROVED_BRIEF:",
            "narrative_format=",
            "VISUAL EVIDENCE CONTRACT",
            "<CHANNEL_PERSONA>",
            "<HUMAN_FEEL>",
        ):
            self.assertIn(marker, prompt)
        schema = providers._gemini_planning_response_schema(prompt)
        self.assertIn("narrative_format", schema["required"])
        self.assertIn("visual_story", schema["required"])

    def test_podcast_prompt_fits_groq_and_keeps_fixed_dialogue_and_quality(self):
        brief = _brief("podcast")
        brief.update({
            "approved_topic": "وهم الكفاءة السريعة: حين يفقد العقل استقلاله بالاتكال المفرط على التقنية",
            "editorial_intent": "برنامج خارج النص: حوار listener-proxy ثابت بالعربية الفصحى الطبيعية وبصوت القناة الثابت، ويقدّم حوارًا حقيقيًا مع مستمع واحد. A بصوت Orus يمثل ذلك المستمع بسؤال أو اعتراض قصير ومحدد عند الحاجة فقط، وB بصوت Charon هو صوت القناة ويحمل الشرح الأساسي. يبدأ الموضوع بسؤال مركزي حقيقي، ثم يجيب B على نفس التوتر مباشرة بعد هوية البرنامج ويتقدم طبقة بعد طبقة حتى يتغير فهم المستمع. لا مضيف/ضيف، لا مجاملات، لا تناوب آلي، لا قائمة نصائح، ولا محاضرة؛ الحلقة يجب أن تبقى مفهومة وممتعة صوتيًا دون الصورة.",
            "hard_constraints": [
                "No fabricated facts.",
                "Use research_pack only within each source claim_scope.",
                "Gemini 3.8 is the only voice provider: Charon is the primary narrator; Orus is allowed only when Planning selects dialogue_qa.",
                "Outside Text uses fixed listener-proxy dialogue: Orus is A (the sparse listener question/objection) and Charon is B (the channel voice carrying the answer).",
                "Every A turn must unlock a genuinely new layer and receive an immediate B answer; never use A as a host, interviewer, or filler speaker.",
                "Outside Text must stay conversational and simple-deep; it must not become a monologue, host/guest interview, lecture, or numbered-list episode, and must never invent first-person experiences.",
                "Selected visuals must remain modest and respectful for a broad Arab/Muslim audience.",
            ],
            "research_pack": [
                {
                    "claim_scope": "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا.",
                    "source_title": "الذكاء الاصطناعي وعقولنا: كيف تسيطر الخوارزميات على تفكيرك؟",
                    "source_url": "https://youtu.be/KDZgqMmE1j4",
                },
                {
                    "claim_scope": "دليل سوقي على وجود محتوى واهتمام حديث حول الموضوع فقط؛ لا يثبت تشخيصًا نفسيًا أو سببية أو نسبة أو ادعاءً علميًا.",
                    "source_title": "هل يتحكم الذكاء الاصطناعي في عقلك الآن؟",
                    "source_url": "https://youtu.be/UJqWOEL7ke8",
                },
            ],
            "series_name": "خارج النص",
        })
        prompt = pipeline._planning_prompt(brief)
        self.assertLess(
            len(prompt.encode("utf-8")),
            providers.GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES,
        )
        for marker in ("narrative_format=dialogue_qa", "The first spoken sentence MUST be A:",
                       "VISUAL VARIETY is semantic, not cosmetic", "POST-HOOK VISUAL FLOOR",
                       "CTA speech and visuals are forbidden"):
            self.assertIn(marker, prompt)

    def test_planner_alternate_is_representable_and_groq_strict_shape_stays_valid(self):
        prompt = pipeline._planning_prompt(_brief("podcast"))
        mistral = providers._mistral_planning_response_schema(prompt)
        gemini = providers._gemini_planning_response_schema(prompt)
        groq = providers._groq_planning_response_schema(prompt)
        for schema in (mistral, groq):
            beat = schema["properties"]["visual_story"]["properties"]["beats"]["items"]
            self.assertIn("stock_query_alt_en", beat["properties"])
        self.assertIn("visual_story", gemini["required"])
        gemini_beat = gemini["properties"]["visual_story"]["properties"]["beats"]["items"]
        self.assertEqual(gemini_beat["type"], "object")
        self.assertTrue(gemini_beat["additionalProperties"])
        def assert_strict(schema):
            if schema.get("type") == "object":
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                self.assertFalse(schema["additionalProperties"])
                for child in schema["properties"].values():
                    assert_strict(child)
            elif schema.get("type") == "array":
                assert_strict(schema["items"])
        assert_strict(groq)
        beat = groq["properties"]["visual_story"]["properties"]["beats"]["items"]
        self.assertEqual(beat["properties"]["stock_query_alt_en"]["type"], ["string", "null"])

    def test_full_script_guide_and_action_validator_are_retained(self):
        brief = _brief("short")
        full = short_format.short_prompt_context(brief)
        planning = short_format.short_prompt_context(brief, for_planning=True)
        self.assertIn("Script self-check: s3 contains zero imperative markers", full)
        self.assertNotIn("Script self-check:", planning)
        self.assertEqual(short_format.validate_short_practical_action("اكتب تحسنًا صغيرًا واحدًا لاحظته اليوم"), "اكتب تحسنًا صغيرًا واحدًا لاحظته اليوم")
        with self.assertRaises(short_format.ShortFormatError) as rejected:
            short_format.validate_short_practical_action("اكتب تحسنًا صغيرًا والتزم بتكراره")
        retry = providers._mistral_planning_validator_retry_prompt("approved plan", rejected.exception)
        self.assertIn("one imperative followed only by its topic-specific object", retry)

    def test_oversized_groq_request_still_skips_without_changing_provider_limit(self):
        calls = []
        router = providers.ProviderRouter((providers.ProviderAdapter(
            "groq",
            lambda *_: calls.append("groq"),
            max_prompt_utf8_bytes=providers.GROQ_MAX_PROMPT_UTF8_BYTES,
            max_prompt_utf8_bytes_by_stage={
                "planning": providers.GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES,
            },
        ),))
        with self.assertRaises(RuntimeError):
            router.route(
                stage="planning",
                prompt="x" * (providers.GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES + 1),
                max_tokens=100,
                validator=lambda value: value,
            )
        self.assertEqual(calls, [])
        self.assertEqual(router.events[0]["reason"], "prompt_too_large_for_provider")


if __name__ == "__main__":
    unittest.main()
