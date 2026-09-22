from __future__ import annotations

import unittest

from clean_v2.pipeline import _validate_script_for_brief
from clean_v2.providers import ProviderAdapter, ProviderRouter
from clean_v2.short_format import SHORT_HOOK_MAX_WORDS, ShortFormatError, validate_short_hook_contract


class ShortHookMax12CohortRegressionTests(unittest.TestCase):
    def test_cohort_15_word_hook_is_rejected_before_provider_output_acceptance(self) -> None:
        brief = {
            "approved_by_user": True,
            "approved_topic": "كيف تنهض عندما تفقد الدافع تمامًا؟",
            "format": "short",
            "language": "ar",
            "audience": "Arabic-speaking adults",
            "editorial_intent": "نبرة هادئة وعملية وطبيعية.",
            "research_pack": [],
            "hard_constraints": ["No fabricated facts."],
        }
        plan = {
            "title": "شورت",
            "promise": "فكرة واحدة واضحة",
            "cta": "",
            "sections": [
                {"id": "s1", "heading": "هوك", "purpose": "شد الانتباه", "visual_query_en": "thoughtful person alone by window"},
                {"id": "s2", "heading": "تحول", "purpose": "تطوير الفكرة", "visual_query_en": "reflective person walking alone quietly"},
                {"id": "s3", "heading": "فعل", "purpose": "خطوة عملية", "visual_query_en": "person writing one small task in notebook"},
            ],
        }
        overlong = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "حين يختفي الدافع لا يعني أنك كسول بل ربما تنتظر شعورًا لن يأتي وحده اليوم أبدًا."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب، فنؤجل الحركة نفسها دون أن نلاحظ."},
                {"id": "s3", "narration": "ابدأ بخطوة صغيرة تستطيع تنفيذها الآن."},
            ],
        }
        valid = {
            "title": "شورت",
            "sections": [
                {"id": "s1", "narration": "قد يختفي الدافع حين تنتظر الشعور قبل أن تبدأ."},
                {"id": "s2", "narration": "أحيانًا نربط البداية بالشعور المناسب، فنؤجل الحركة نفسها دون أن نلاحظ."},
                {"id": "s3", "narration": "ابدأ بخطوة صغيرة تستطيع تنفيذها الآن."},
            ],
        }

        self.assertEqual(SHORT_HOOK_MAX_WORDS, 12)
        with self.assertRaisesRegex(ShortFormatError, r"short_hook_too_long words=15 maximum=12"):
            validate_short_hook_contract(overlong)

        router = ProviderRouter((
            ProviderAdapter("groq", lambda _prompt, _tokens: overlong),
            ProviderAdapter("mistral", lambda _prompt, _tokens: valid),
        ))
        accepted = router.route(
            stage="script",
            prompt="cohort-attempt-2-hook-regression",
            max_tokens=400,
            validator=lambda value: _validate_script_for_brief(value, plan, brief),
        )
        self.assertEqual(accepted, valid)
        self.assertEqual(
            [(event["provider"], event["result"]) for event in router.events],
            [("groq", "invalid_output"), ("mistral", "success")],
        )


if __name__ == "__main__":
    unittest.main()
