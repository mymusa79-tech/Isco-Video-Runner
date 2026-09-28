from __future__ import annotations

import inspect
import unittest

from clean_v2 import cover_lite, cover_studio, media, podcast_key_text, short_timed_text
from clean_v2.contracts import LONGFORM_NARRATIVE_FORMATS
from clean_v2.pipeline import (
    _bound_ai_still_preferences,
    _bound_short_visual_story,
    _planning_prompt,
    _script_prompt,
    _select_longform_narrative_profile,
    _voice_performance_mode_for_brief,
)
from clean_v2.short_format import (
    TEMPLATE_ORDER,
    select_short_template,
    short_prompt_context,
)
from clean_v2.visual_story import (
    _visual_action_family,
    bind_visual_story_to_script,
    contextual_intent,
)


RUNTIME_FEATURE_IDS = (
    "short_why_reframe",
    "short_inner_dialogue",
    "short_micro_story",
    "short_quote_reflection",
    "film_direct_cinematic",
    "film_question_answer",
    "film_dialogue_qa",
    "film_inner_dialogue",
    "film_problem_reveal_solution",
    "film_story_analysis",
    "film_paradox",
    "film_hypothesis_test",
    "film_connected_list",
    "podcast_kharij_alnass_fixed_house_style",
    "shared_hook_coverage",
    "shared_scene_family_collapse",
    "shared_writer_bound_alternate",
    "shared_visual_qa_family_context",
    "short_three_to_five_semantic_beats",
    "shared_ai_inside_scene_budget",
    "shared_same_page_semantic_stock_rank",
    "shared_channel_anchored_color_reference",
    "shared_one_pass_stock_motion",
    "shared_cairo_two_line_video_typography",
    "shared_cairo_strong_cover_typography",
    "shared_type_to_gemini_performance_runtime",
)


def _brief(fmt: str, topic: str, **extra: object) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": topic,
        "format": fmt,
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": str(extra.pop("editorial_intent", "واضح، عميق، عملي، ومتزن")),
        "research_pack": list(extra.pop("research_pack", [])),
        "hard_constraints": ["No fabricated facts."],
        **extra,
    }


SHORT_CASES = {
    "why_reframe": _brief("short", "لماذا المشكلة ليست في الوقت كما تظن؟"),
    "inner_dialogue": _brief("short", "قلت لنفسي: ماذا لو كان فقد الدافع مجرد بداية أصعب؟"),
    "micro_story": _brief("short", "قصة شخص بدأ من جديد بعد يوم صعب"),
    "quote_reflection": _brief("short", "هذه العبارة «ابدأ قبل أن تشعر بالاستعداد» تستحق وقفة"),
}


FILM_CASES = {
    "direct_cinematic": _brief("film", "البداية الصغيرة التي تغيّر مسار يومك"),
    "question_answer": _brief("film", "لماذا نؤجل قرارًا نعرف أنه مهم؟"),
    "dialogue_qa": _brief("film", "حوار حول اعتراضين على تغيير العادات"),
    "inner_dialogue": _brief("film", "قلت لنفسي ماذا لو أبدأ من جديد؟"),
    "problem_reveal_solution": _brief("film", "لماذا تفشل خطط إدارة الوقت وما السبب والحل؟"),
    "story_analysis": _brief("film", "قصة شخص سقط ثم عاد وبدأ من جديد"),
    "paradox": _brief("film", "مفارقة: كلما حاولت السيطرة أكثر زاد التشتت"),
    "hypothesis_test": _brief("film", "هل فعلًا صحيح أن البداية الصغيرة تغيّر السلوك؟"),
    "connected_list": _brief("film", "5 أسباب تجعل العادة أسهل"),
}


def _plan(section_count: int = 3) -> dict:
    return {
        "title": "اختبار runtime",
        "promise": "فهم الفكرة بوضوح",
        "cta": "",
        "sections": [
            {
                "id": f"s{i}",
                "heading": f"قسم {i}",
                "purpose": f"إضافة معنى جديد {i}",
                "visual_query_en": f"person doing distinct action {i} no face",
                "visual_query_alt_en": f"person walking through corridor {i} no face",
            }
            for i in range(1, section_count + 1)
        ],
    }


class RuntimeActivation26Tests(unittest.TestCase):
    def test_feature_manifest_is_exactly_26_unique_runtime_contracts(self) -> None:
        self.assertEqual(len(RUNTIME_FEATURE_IDS), 26)
        self.assertEqual(len(set(RUNTIME_FEATURE_IDS)), 26)

    # 01-04: every Short type must be actually selectable from approved input
    # and must inject its own writer + visual grammar into the existing prompt.
    def _assert_short_type(self, expected: str) -> None:
        brief = SHORT_CASES[expected]
        selected = select_short_template(brief)
        self.assertEqual(selected["template"], expected)
        self.assertIn(expected, TEMPLATE_ORDER)
        context = short_prompt_context(brief)
        self.assertIn(f"selected_template={expected}", context)
        self.assertIn(selected["writing_directive"], context)
        self.assertIn(selected["visual_query_directive"], context)
        self.assertEqual(_voice_performance_mode_for_brief(brief, None), expected)

    def test_01_short_why_reframe_runtime(self) -> None:
        self._assert_short_type("why_reframe")

    def test_02_short_inner_dialogue_runtime(self) -> None:
        self._assert_short_type("inner_dialogue")

    def test_03_short_micro_story_runtime(self) -> None:
        self._assert_short_type("micro_story")

    def test_04_short_quote_reflection_runtime(self) -> None:
        self._assert_short_type("quote_reflection")

    # 05-13: every Film shape must be reachable deterministically and must be
    # injected into both planning and writing, not stored as dead metadata.
    def _assert_film_type(self, expected: str) -> None:
        brief = FILM_CASES[expected]
        selected = _select_longform_narrative_profile(brief)
        self.assertEqual(selected["narrative_format"], expected)
        self.assertIn(expected, LONGFORM_NARRATIVE_FORMATS)
        planning = _planning_prompt(brief)
        self.assertIn(f"narrative_format={expected}", planning)
        self.assertIn(selected["visual"], planning)
        plan = _plan(5)
        plan["narrative_format"] = expected
        writing = _script_prompt(brief, plan)
        self.assertIn(f"narrative_format={expected}", writing)
        self.assertIn(selected["writing"], writing)
        self.assertIn(selected["visual"], writing)
        self.assertEqual(_voice_performance_mode_for_brief(brief, plan), expected)

    def test_05_film_direct_cinematic_runtime(self) -> None:
        self._assert_film_type("direct_cinematic")

    def test_06_film_question_answer_runtime(self) -> None:
        self._assert_film_type("question_answer")

    def test_07_film_dialogue_qa_runtime(self) -> None:
        self._assert_film_type("dialogue_qa")

    def test_08_film_inner_dialogue_runtime(self) -> None:
        self._assert_film_type("inner_dialogue")

    def test_09_film_problem_reveal_solution_runtime(self) -> None:
        self._assert_film_type("problem_reveal_solution")

    def test_10_film_story_analysis_runtime(self) -> None:
        self._assert_film_type("story_analysis")

    def test_11_film_paradox_runtime(self) -> None:
        self._assert_film_type("paradox")

    def test_12_film_hypothesis_test_runtime(self) -> None:
        self._assert_film_type("hypothesis_test")

    def test_13_film_connected_list_runtime(self) -> None:
        self._assert_film_type("connected_list")

    # 14: خارج النص is intentionally independent of Film rotation.
    def test_14_podcast_fixed_house_style_runtime(self) -> None:
        for topic in (
            "لماذا نفقد الدافع؟",
            "مفارقة السيطرة والهدوء",
            "5 أسباب تجعلنا نؤجل",
        ):
            brief = _brief("podcast", topic)
            selected = _select_longform_narrative_profile(brief)
            self.assertEqual(selected["selection_basis"], "podcast_fixed_house_style")
            self.assertEqual(selected["narrative_format"], "dialogue_qa")
            self.assertEqual(selected["voice"], "podcast_listener_proxy_qa")
            self.assertEqual(
                _voice_performance_mode_for_brief(brief, _plan()),
                "podcast_listener_proxy_qa",
            )
            planning = _planning_prompt(brief)
            writing = _script_prompt(brief, _plan())
            self.assertIn("خارج النص", planning)
            self.assertIn("listener-proxy", writing)

    # 15: shared hook grammar is injected for all three production formats.
    def test_15_shared_hook_coverage_runtime(self) -> None:
        for fmt in ("short", "film", "podcast"):
            prompt = _planning_prompt(_brief(fmt, "لماذا نؤجل البداية المهمة؟"))
            self.assertIn("HOOK COVERAGE CONTRACT", prompt)
            self.assertIn("unresolved", prompt)
            self.assertIn("first body beat", prompt)

    # 16: semantic family collapse closes notebook/sticky/checklist false diversity.
    def test_16_shared_scene_family_collapse_runtime(self) -> None:
        for text in (
            "hand writing in notebook with pen",
            "sticky notes on paper planner",
            "checklist on journal page",
        ):
            self.assertEqual(_visual_action_family(text), "stationery")
        self.assertEqual(_visual_action_family("person typing on laptop keyboard"), "typing")

    # 17: Writer-bound alternate is the query actually consumed after a repeat.
    def test_17_shared_writer_bound_alternate_runtime(self) -> None:
        plan = _plan(3)
        story = {
            "beats": [
                {"id": "b1", "section_id": "s1", "role": "hook", "shot_intent": "hand writing in notebook", "stock_query_en": "hand writing in notebook"},
                {"id": "b2", "section_id": "s2", "role": "body", "shot_intent": "sticky notes on planner page", "stock_query_en": "sticky notes on planner page"},
                {"id": "b3", "section_id": "s3", "role": "payoff", "shot_intent": "hand closes completed notebook", "stock_query_en": "hand closes completed notebook"},
            ]
        }
        plan["sections"][1]["visual_query_alt_en"] = "person walking through quiet corridor no face"
        script = {
            "sections": [
                {"id": "s1", "narration": "تبدو البداية ثقيلة حين يبقى كل شيء أمامك دفعة واحدة."},
                {"id": "s2", "narration": "يتغير الإحساس عندما تتحرك إلى سياق مختلف وواضح."},
                {"id": "s3", "narration": "هنا يصبح التقدم مرئيًا بدل أن يبقى مجرد نية."},
            ]
        }
        bound = bind_visual_story_to_script(story, plan, script)
        self.assertIn("walking", bound["beats"][1]["shot_intent"])
        self.assertEqual(bound["beats"][1]["stock_query_en"], bound["beats"][1]["shot_intent"])

    # 18: existing Visual QA receives the role and neighboring family context.
    def test_18_shared_visual_qa_family_context_runtime(self) -> None:
        story = {
            "beats": [
                {"id": "b1", "role": "hook", "shot_intent": "hand frozen above notebook", "meaning_target": "hesitation"},
                {"id": "b2", "role": "body", "shot_intent": "sticky note on planner", "meaning_target": "same friction"},
            ]
        }
        hook = contextual_intent(story, "b1", "")
        body = contextual_intent(story, "b2", "")
        self.assertIn("Role:hook", hook)
        self.assertIn("Fam:stationery", hook)
        self.assertIn("PrevFam:stationery", body)
        self.assertIn("Repeat:", body)

    # 19: Short never re-expands to the retired 6-9 cut target.
    def test_19_short_three_to_five_semantic_beats_runtime(self) -> None:
        story = {
            "beats": [
                {"id": f"b{i}", "section_id": "s1" if i <= 3 else ("s2" if i <= 5 else "s3"), "role": "body"}
                for i in range(1, 8)
            ]
        }
        story["beats"][0]["role"] = "hook"
        story["beats"][-1]["role"] = "payoff"
        bounded = _bound_short_visual_story(story, max_beats=5)
        self.assertGreaterEqual(len(bounded["beats"]), 3)
        self.assertLessEqual(len(bounded["beats"]), 5)
        self.assertEqual(bounded["beats"][0]["role"], "hook")
        self.assertEqual(bounded["beats"][-1]["role"], "payoff")

    # 20: free AI is inside the same scene budget; it never creates extra beats.
    def test_20_shared_ai_inside_scene_budget_runtime(self) -> None:
        base = {"beats": [
            {"id": "b1", "role": "hook", "source_preference": "ai_still"},
            {"id": "b2", "role": "body", "source_preference": "ai_still"},
            {"id": "b3", "role": "body", "source_preference": "ai_still"},
            {"id": "b4", "role": "payoff", "source_preference": "ai_still"},
        ]}
        for fmt in ("short", "film", "podcast"):
            bounded = _bound_ai_still_preferences(base, fmt=fmt)
            self.assertEqual(len(bounded["beats"]), 4)
            self.assertLessEqual(
                sum(beat["source_preference"] == "ai_still" for beat in bounded["beats"]),
                2,
            )

    # 21: same provider page gets a zero-call semantic tie-break.
    def test_21_shared_same_page_semantic_stock_rank_runtime(self) -> None:
        common = dict(index=3, count=12, width=1080, height=1920, duration=6.0, portrait=True, query="hands frozen above empty notebook pen paper")
        matching = media._stock_local_rank_score(**common, metadata="hands pen empty notebook paper")
        generic = media._stock_local_rank_score(**common, metadata="sunset ocean travel landscape")
        self.assertGreater(matching, generic)

    # 22: stock cannot redefine the channel color world through a warm medoid.
    def test_22_shared_channel_anchored_color_reference_runtime(self) -> None:
        measured = {
            "neutral.mp4": media._RgbStats(141.2, 131.7, 119.7, 76.0, 75.0, 80.5),
            "warm.mp4": media._RgbStats(179.7, 138.6, 108.4, 56.4, 57.5, 54.7),
            "bright.mp4": media._RgbStats(184.0, 150.8, 120.3, 43.0, 42.7, 40.4),
        }
        self.assertEqual(media._representative_reference(measured), "neutral.mp4")

    # 23: every real-stock render path is one-pass; shortage holds last frame.
    def test_23_shared_one_pass_stock_motion_runtime(self) -> None:
        render_source = inspect.getsource(media.render_video)
        trim_source = inspect.getsource(media._trim_and_grade_clip)
        body_source = inspect.getsource(media._build_section_body_segments)
        self.assertNotIn("-stream_loop", render_source)
        self.assertIn("tpad=stop_mode=clone", render_source)
        self.assertIn("tpad=stop_mode=clone", trim_source)
        self.assertNotIn("short_motion_lite", body_source)
        self.assertNotIn("motion_mode", body_source)
        self.assertNotIn('("push", "pan", "pull")', body_source)

    # 24: in-video text has one shared readable identity, not boxes or 3+ lines.
    def test_24_shared_cairo_two_line_video_typography_runtime(self) -> None:
        self.assertEqual(short_timed_text.BODY_FONT, "Cairo")
        self.assertEqual(short_timed_text.MAX_CAPTION_LINES, 2)
        wrapped = short_timed_text._plain_caption(
            "هذه جملة عربية واضحة تحمل فكرة كاملة وتبقى مقروءة على الشاشة دائمًا"
        )
        self.assertLessEqual(wrapped.count(r"\N"), 1)
        hook = short_timed_text._font_size_for_event(
            short_timed_text.TimedTextEvent(0.0, 2.0, "لماذا نؤجل البداية؟", "hook")
        )
        beat = short_timed_text._font_size_for_event(
            short_timed_text.TimedTextEvent(2.0, 4.0, "نفهم السبب بهدوء", "beat")
        )
        self.assertGreater(hook, beat)
        for fmt in ("film", "podcast"):
            self.assertGreater(
                podcast_key_text._event_font_size("hook", fmt=fmt),
                podcast_key_text._event_font_size("turn", fmt=fmt),
            )
            ass = podcast_key_text.build_ass(
                [
                    {"start": 0.0, "end": 2.0, "text": "لماذا نؤجل البداية المهمة؟", "role": "hook"},
                    {"start": 3.0, "end": 5.0, "text": "هنا يبدأ الفهم الحقيقي", "role": "payoff"},
                ],
                fmt=fmt,
            )
            self.assertIn("Style: Caption,Cairo", ass)
            self.assertNotIn("drawbox", ass)

    # 25: cover keeps editorial freedom but the font identity is locked locally.
    def test_25_shared_cairo_strong_cover_typography_runtime(self) -> None:
        self.assertEqual(cover_studio._FONT_NAMES["black"], ("Cairo.ttf",))
        self.assertEqual(cover_studio._FONT_NAMES["bold"], ("Cairo.ttf",))
        studio_source = inspect.getsource(cover_studio._render_line)
        self.assertIn("stroke_width", studio_source)
        self.assertIn("EXTRUSION", studio_source)
        fallback_source = inspect.getsource(cover_lite._write_ass)
        self.assertIn("Cairo", fallback_source)

    # 26: the selected editorial type reaches the actual voice call.
    def test_26_shared_type_to_gemini_performance_runtime(self) -> None:
        run_source = inspect.getsource(__import__("clean_v2.pipeline", fromlist=["CleanV2Pipeline"]).CleanV2Pipeline.run)
        self.assertIn("performance_mode=_voice_performance_mode_for_brief", run_source)

        for expected, brief in SHORT_CASES.items():
            self.assertEqual(_voice_performance_mode_for_brief(brief, None), expected)
        for expected, brief in FILM_CASES.items():
            self.assertEqual(
                _voice_performance_mode_for_brief(brief, {"narrative_format": expected}),
                expected,
            )

        podcast = _brief("podcast", "لماذا نخفي السؤال الذي نحتاج أن نسأله؟")
        self.assertEqual(
            _voice_performance_mode_for_brief(podcast, _plan()),
            "podcast_listener_proxy_qa",
        )
        roles = media._spoken_voice_roles("A: لماذا يحدث هذا؟\nB: لأننا نخلط بين الشعور والفعل.")
        self.assertEqual(roles["mode"], "dialogue_qa")
        self.assertEqual(roles["questioner"], "Orus")
        self.assertEqual(roles["responder"], "Charon")
        mono = media._spoken_voice_roles("أفكر في السؤال ثم أصل إلى جواب أوضح.")
        self.assertEqual(mono["mode"], "single_narrator")
        self.assertEqual(mono["narrator"], "Charon")


if __name__ == "__main__":
    unittest.main()
