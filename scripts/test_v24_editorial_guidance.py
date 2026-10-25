"""V24 editorial prompts must affect both short planning and writing."""
import unittest

from clean_v2.short_format import (
    SHORT_SECTION_COUNT, SHORT_DURATION_SAFETY_MAX_SECONDS,
    V24_CHANNEL_FIT_CHECK, V24_VISUAL_EDITORIAL_CHECK,
    short_prompt_context, select_short_template,
)
from clean_v2.visual_story import CHANNEL_VISUAL_AVOID


class V24EditorialGuidanceTests(unittest.TestCase):
    def setUp(self):
        self.brief = {"format": "short", "approved_topic": "لماذا تتردد في قول لا أعرف؟"}

    def test_planning_includes_non_generic_channel_and_visual_directions(self):
        prompt = short_prompt_context(self.brief, for_planning=True)
        self.assertIn(V24_CHANNEL_FIT_CHECK, prompt)
        self.assertIn(V24_VISUAL_EDITORIAL_CHECK, prompt)
        self.assertIn("CHANNEL_FIT_V24", prompt)
        self.assertIn("SHOT_QUALITY_V24", prompt)

    def test_script_includes_same_channel_and_visual_directions(self):
        prompt = short_prompt_context(self.brief)
        self.assertIn(V24_CHANNEL_FIT_CHECK, prompt)
        self.assertIn(V24_VISUAL_EDITORIAL_CHECK, prompt)
        self.assertIn("observable event/state", prompt)
        self.assertIn("exact_sections=3", prompt)

    def test_visual_global_avoid_rejects_symbolic_filler(self):
        combined = " ".join(CHANNEL_VISUAL_AVOID).lower()
        self.assertIn("old watches", combined)
        self.assertIn("tungsten", combined)
        self.assertIn("blanket navy", combined)

    def test_no_schema_or_provider_call_change(self):
        self.assertEqual(SHORT_SECTION_COUNT, 3)
        self.assertEqual(SHORT_DURATION_SAFETY_MAX_SECONDS, 120.0)
        self.assertEqual(select_short_template(self.brief)["extra_ai_calls"], 0)


if __name__ == "__main__":
    unittest.main()
