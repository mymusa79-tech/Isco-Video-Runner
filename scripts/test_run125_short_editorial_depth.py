"""Run 125: a real Short needs depth, and a scenic prop is not a usable action.

No new provider calls, duration limits, or script-word gates are introduced.
"""
from __future__ import annotations

import inspect
import unittest

from clean_v2.pipeline import _script_prompt
from clean_v2.short_format import (
    ShortFormatError,
    short_prompt_context,
    validate_short_practical_action_specificity,
)


class Run125ShortEditorialDepthTests(unittest.TestCase):
    def test_run125_locked_action_cannot_launder_prop_through_plan(self) -> None:
        with self.assertRaisesRegex(ShortFormatError, "short_practical_action_unrelated_staged_prop"):
            validate_short_practical_action_specificity(
                "ضع مفتاحًا واحدًا على سطح الطاولة لتبدأ يومك بوضوح.",
                topic_context="مفتاح على سطح الطاولة ومشهد يوم لم تنجز فيه شيئًا",
                approved_topic="كيف تتعامل مع يوم لم تنجز فيه شيئًا؟",
            )

    def test_prop_placement_remains_valid_for_actual_prop_topic(self) -> None:
        action = "ضع مفتاحًا واحدًا على سطح الطاولة."
        self.assertEqual(
            validate_short_practical_action_specificity(
                action,
                topic_context="مفتاح على سطح الطاولة",
                approved_topic="أين تضع مفتاح المنزل حتى لا تنساه؟",
            ),
            action,
        )

    def test_nonprop_topic_specific_daily_action_stays_valid(self) -> None:
        action = "اكتب عنوان تقرير الغد."
        self.assertEqual(
            validate_short_practical_action_specificity(
                action,
                topic_context="كتابة عنوان تقرير الغد",
                approved_topic="كيف تنجز تقرير العمل دون تسويف؟",
            ),
            action,
        )

    def test_short_planning_warns_against_scenic_props(self) -> None:
        context = short_prompt_context({"approved_topic": "كيف تنهض بعد يوم بلا إنجاز؟"}, for_planning=True)
        self.assertIn("Never promote a scenic prop", context)
        self.assertIn("independently of the visual story", context)

    def test_short_writer_has_depth_not_a_new_word_gate(self) -> None:
        source = inspect.getsource(_script_prompt)
        self.assertIn("85-115 meaningful authored Arabic words", source)
        self.assertIn("not a new word-count gate", source)
        self.assertIn("s2 add the causal turn or concrete distinction", source)
        self.assertIn("A section made of a single slogan is underdeveloped", source)


if __name__ == "__main__":
    unittest.main()
