"""V24 visual and editorial prompt regression checks; no provider calls."""
import unittest

from clean_v2.short_format import (
    SHORT_DURATION_SAFETY_MAX_SECONDS,
    SHORT_HOOK_MAX_WORDS,
    short_prompt_context,
)
from clean_v2.pipeline import _PODCAST_FIXED_PROFILE
from clean_v2.visual_story import CHANNEL_VISUAL_IDENTITY


class V24EditorialPromptTests(unittest.TestCase):
    def test_short_planning_and_script_inherit_meaning_led_guidance(self):
        brief = {"format": "short", "approved_topic": "لماذا تؤجل سؤالًا بسيطًا في عملك؟"}
        for planning in (True, False):
            with self.subTest(planning=planning):
                prompt = short_prompt_context(brief, for_planning=planning)
                self.assertIn("V24 NIDAA IDENTITY", prompt)
                self.assertIn("V24 VISUAL DIRECTOR", prompt)
                self.assertIn("observable", prompt.lower())
                self.assertIn("earned", prompt.lower())

    def test_podcast_stays_dialogue_but_visuals_not_staged_drama(self):
        profile = _PODCAST_FIXED_PROFILE
        self.assertEqual(profile["narrative_format"], "dialogue_qa")
        self.assertIn("editorial documentary", profile["visual"])
        self.assertIn("fake narrative props", profile["visual"])
        self.assertIn("natural", profile["visual"])

    def test_visual_identity_is_natural_not_blue_washed(self):
        self.assertIn("natural daylight", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("navy and gold", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("envelopes", CHANNEL_VISUAL_IDENTITY)
        self.assertIn("modest dress", CHANNEL_VISUAL_IDENTITY)

    def test_no_existing_short_duration_or_hook_contract_changed(self):
        self.assertEqual(SHORT_DURATION_SAFETY_MAX_SECONDS, 120.0)
        self.assertEqual(SHORT_HOOK_MAX_WORDS, 18)


if __name__ == "__main__":
    unittest.main()
