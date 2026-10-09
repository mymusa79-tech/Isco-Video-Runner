import unittest

from clean_v2.visual_story import visual_review_context


class PrimaryProofContextTests(unittest.TestCase):
    def _story(self, cues):
        return {"beats": [{"id": "b2", "role": "hook", "shot_intent": "x", "meaning_target": "m",
                           "semantic_must_have": cues, "semantic_should_avoid": ["logos"]}]}

    def test_only_first_cue_is_required_rest_optional(self):
        text = visual_review_context(self._story(["half-empty cup in hands", "stormy window", "open book"]), "b2", "fb")
        self.assertIn("Must show:half-empty cup in hands.", text)
        self.assertIn("Optional support (NOT required for PROOF: matched): stormy window, open book", text)

    def test_single_cue_has_no_optional_section(self):
        text = visual_review_context(self._story(["puzzle piece fitting"]), "b2", "fb")
        self.assertIn("Must show:puzzle piece fitting", text)
        self.assertNotIn("Optional support", text)


if __name__ == "__main__":
    unittest.main()
