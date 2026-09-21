from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2.contextual_cta import CtaMode, bind_contextual_cta
from clean_v2.contracts import ContractError, validate_plan
from clean_v2.opening_director import run_opening_director
from clean_v2.pipeline import (
    _planning_prompt,
    _script_prompt,
    _short_identity_not_applicable,
    _validate_plan_for_brief,
)
from clean_v2.short_format import (
    SHORT_HEIGHT,
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    SHORT_SECTION_COUNT,
    SHORT_TARGET_SECONDS,
    SHORT_WIDTH,
    ShortFormatError,
    TEMPLATE_VISUAL_QUERY_DIRECTIVES,
    select_short_template,
    short_contract_report,
    short_prompt_context,
    validate_short_dimensions,
    validate_short_duration,
    validate_short_script,
    validate_short_visual_queries,
)


def _brief(topic: str, *, editorial_intent: str = "", research_pack=None) -> dict:
    return {
        "approved_by_user": True,
        "approved_topic": topic,
        "format": "short",
        "language": "ar",
        "audience": "Arabic-speaking adults",
        "editorial_intent": editorial_intent or "نبرة هادئة وعملية وطبيعية.",
        "research_pack": list(research_pack or []),
        "hard_constraints": ["No fabricated facts."],
    }


def _plan(queries: list[str]) -> dict:
    return {
        "title": "شورت تجريبي",
        "promise": "فكرة واحدة واضحة وقابلة للتطبيق",
        "cta": "",
        "sections": [
            {
                "id": f"s{index}",
                "heading": f"قسم {index}",
                "purpose": f"غرض القسم {index}",
                "visual_query_en": query,
            }
            for index, query in enumerate(queries, 1)
        ],
    }


_TEMPLATE_FIXTURES = {
    "why_reframe": {
        "brief": _brief("لماذا نخطئ عندما نظن أن الخطة المثالية تكفي؟"),
        "queries": [
            "confused worker staring at cluttered daily schedule",
            "person pause reconsidering written plan at desk",
            "calm focused worker using simple practical schedule",
        ],
    },
    "inner_dialogue": {
        "brief": _brief("كيف تنهض عندما تفقد الدافع وتقول لنفسك لا أستطيع؟"),
        "queries": [
            "person alone thoughtful quiet moment by window",
            "solitary person thinking in calm quiet room",
            "reflective person walking alone peaceful morning",
        ],
    },
    "micro_story": {
        "brief": _brief("قصة قصيرة: ذات يوم بدأت تجربة صغيرة ثم تغيرت النتيجة"),
        "queries": [
            "woman opening notebook at quiet desk",
            "woman writing notebook task list at desk",
            "woman closing notebook after finishing work",
        ],
    },
    "quote_reflection": {
        "brief": _brief("اقتباس للتأمل: «ابدأ بما تستطيع اليوم»"),
        "queries": [
            "quiet reflective room with soft morning light",
            "calm person reading slowly in minimal room",
            "peaceful contemplative window scene with still light",
        ],
    },
}


class ShortTemplateSelectionTests(unittest.TestCase):
    def test_all_four_templates_are_reachable_deterministically(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                selection = select_short_template(fixture["brief"])
                self.assertEqual(selection["template"], expected)
                self.assertEqual(selection["extra_ai_calls"], 0)
                self.assertIn(expected, TEMPLATE_VISUAL_QUERY_DIRECTIVES)

    def test_quote_reflection_requires_real_quote_evidence(self) -> None:
        brief = _brief("هذه عبارة جميلة للتأمل")
        selection = select_short_template(brief)
        self.assertNotEqual(selection["template"], "quote_reflection")

    def test_template_specific_visual_queries_are_enforced(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                plan = _plan(fixture["queries"])
                report = validate_short_visual_queries(plan, fixture["brief"])
                self.assertEqual(report["template"], expected)
                self.assertEqual(report["status"], "pass")
                self.assertEqual(len(report["queries"]), 3)

    def test_inner_dialogue_rejects_generic_visual_queries(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        generic = _plan(
            [
                "desk calendar and coffee cup",
                "city street wide establishing shot",
                "generic office laptop workspace",
            ]
        )
        with self.assertRaisesRegex(
            ShortFormatError,
            "inner_dialogue_not_reflective",
        ):
            validate_short_visual_queries(generic, fixture["brief"])

    def test_planning_prompt_contains_selected_template_visual_direction_without_extra_call(self) -> None:
        for expected, fixture in _TEMPLATE_FIXTURES.items():
            with self.subTest(template=expected):
                prompt = _planning_prompt(fixture["brief"])
                selection = select_short_template(fixture["brief"])
                self.assertIn("Use exactly 3 sections", prompt)
                self.assertIn("VISUAL_QUERY_DIRECTION", prompt)
                self.assertIn(selection["visual_query_directive"], prompt)
                self.assertIn(f"selected_template={expected}", prompt)
                self.assertIn("return an empty CTA string", prompt)
                self.assertEqual(selection["extra_ai_calls"], 0)


class ShortContractTests(unittest.TestCase):
    def test_plan_requires_exactly_three_sections_and_empty_social_cta(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        valid = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        normalized = validate_plan(valid, brief)
        self.assertEqual(len(normalized["sections"]), SHORT_SECTION_COUNT)
        self.assertEqual(normalized["cta"], "")

        too_short = _plan(valid["sections"] and _TEMPLATE_FIXTURES["inner_dialogue"]["queries"][:2])
        with self.assertRaisesRegex(ContractError, "exactly 3 for short"):
            validate_plan(too_short, brief)

        with_cta = _plan(_TEMPLATE_FIXTURES["inner_dialogue"]["queries"])
        with_cta["cta"] = "اشترك في القناة"
        with self.assertRaisesRegex(ContractError, "empty social cta"):
            validate_plan(with_cta, brief)

    def test_combined_plan_validator_checks_visual_template_fit(self) -> None:
        brief = _TEMPLATE_FIXTURES["inner_dialogue"]["brief"]
        with self.assertRaisesRegex(ShortFormatError, "inner_dialogue_not_reflective"):
            _validate_plan_for_brief(
                _plan(
                    [
                        "desk calendar and coffee cup",
                        "city street wide establishing shot",
                        "generic office laptop workspace",
                    ]
                ),
                brief,
            )

    def test_script_contract_requires_hook_single_voice_and_zero_social_cta(self) -> None:
        valid = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد لا تكون المشكلة في الدافع نفسه. حين تتوقف قليلًا ترى ما يحدث بوضوح."},
                {"id": "s2", "narration": "الفكرة الصغيرة هنا أن تلاحظ اللحظة التي تنسحب فيها من الفعل، دون لوم أو مبالغة."},
                {"id": "s3", "narration": "اختر حركة بسيطة تستطيع تنفيذها الآن، ثم دع الخطوة التالية تأتي بعد أن تبدأ."},
            ],
        }
        report = validate_short_script(valid)
        self.assertTrue(report["single_voice"])
        self.assertFalse(report["social_cta"])
        self.assertLessEqual(report["hook_words"], 12)

        dialogue = json.loads(json.dumps(valid, ensure_ascii=False))
        dialogue["sections"][0]["narration"] = "A: هل أبدأ الآن؟ B: نعم، بخطوة واحدة واضحة."
        with self.assertRaisesRegex(ShortFormatError, "single_voice"):
            validate_short_script(dialogue)

        cta = json.loads(json.dumps(valid, ensure_ascii=False))
        cta["sections"][2]["narration"] += " اشترك في القناة."
        with self.assertRaisesRegex(ShortFormatError, "zero_social_cta"):
            validate_short_script(cta)

    def test_duration_and_frame_contract_are_hard_bounds(self) -> None:
        for seconds in (SHORT_MIN_SECONDS, SHORT_TARGET_SECONDS, SHORT_MAX_SECONDS):
            self.assertEqual(validate_short_duration(seconds, phase="test"), seconds)
        for seconds in (SHORT_MIN_SECONDS - 0.001, SHORT_MAX_SECONDS + 0.001):
            with self.assertRaisesRegex(ShortFormatError, "short_duration_out_of_range"):
                validate_short_duration(seconds, phase="test")

        self.assertEqual(
            validate_short_dimensions(SHORT_WIDTH, SHORT_HEIGHT),
            (1080, 1920),
        )
        with self.assertRaisesRegex(ShortFormatError, "short_frame_mismatch"):
            validate_short_dimensions(1920, 1080)

    def test_contract_report_records_no_identity_no_opening_and_no_social_cta(self) -> None:
        report = short_contract_report(_TEMPLATE_FIXTURES["why_reframe"]["brief"])
        self.assertEqual(report["format"], "short")
        self.assertEqual(report["section_count"], 3)
        self.assertEqual(report["frame"], {"width": 1080, "height": 1920})
        self.assertEqual(report["social_cta"], "forbidden")
        self.assertEqual(report["narrative_identity"], "not_applicable")
        self.assertEqual(report["opening_director"], "not_applicable")


class ShortPipelineSeamTests(unittest.TestCase):
    def test_short_script_prompt_targets_75_seconds_and_reuses_template_context(self) -> None:
        fixture = _TEMPLATE_FIXTURES["inner_dialogue"]
        prompt = _script_prompt(fixture["brief"], _plan(fixture["queries"]))
        self.assertIn("145-185 spoken Arabic words", prompt)
        self.assertIn("near 75 seconds", prompt)
        self.assertIn("selected_template=inner_dialogue", prompt)
        self.assertIn("CTA is", prompt)
        self.assertIn("fully disabled", prompt)

    def test_short_identity_is_local_not_applicable_with_zero_provider_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            report = _short_identity_not_applicable(output)
            self.assertEqual(report["status"], "not_applicable")
            self.assertEqual(report["provider_calls_added"], 0)
            persisted = json.loads(
                (output / "narrative-identity.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["transitions"], [])
            self.assertEqual(persisted["opener"], "")
            self.assertEqual(persisted["closer"], "")

    def test_opening_director_short_is_local_not_applicable_before_any_provider_use(self) -> None:
        router = mock.Mock()
        visual_source = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            report = run_opening_director(
                output_dir=Path(temporary),
                plan={"sections": []},
                script={"sections": []},
                rights=[],
                fmt="short",
                narration_path=Path(temporary) / "missing.wav",
                visual_source=visual_source,
                router=router,
            )
        self.assertEqual(report["status"], "not_applicable")
        router.assert_not_called()
        visual_source.assert_not_called()

    def test_contextual_cta_binding_is_none_for_short(self) -> None:
        plan = SimpleNamespace(
            format="short",
            cta="اشترك في القناة",
            sections=[SimpleNamespace(id="s1", narration="نص تجريبي")],
        )
        binding = bind_contextual_cta(plan)
        self.assertEqual(binding.mode, CtaMode.NONE)
        self.assertEqual(binding.spoken_text, "")
        self.assertEqual(binding.reason, "short_no_cta")


if __name__ == "__main__":
    unittest.main()
