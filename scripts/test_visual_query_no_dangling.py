import unittest

from clean_v2.visual_story import _writer_searchable_intent, visual_review_context


class DanglingQueryTests(unittest.TestCase):
    def test_long_query_never_ends_on_connector_or_modifier(self):
        for text in (
            "messy cluttered desk with a laptop computer showing an error message and half-empty coffee cup",
            "close-up of hands typing on laptop at messy desk, screen showing error message, half-empty coffee mug",
        ):
            out = _writer_searchable_intent(text).split()
            self.assertGreaterEqual(len(out), 3)
            self.assertNotIn(out[-1], {"showing", "displaying", "holding", "with", "and"})
            self.assertNotIn("-", out[-1])

    def test_face_cues_stay_out_of_review_support(self):
        story = {"beats": [{"id": "b", "semantic_must_have": ["hands typing", "smiling person", "coffee cup"]}]}
        ctx = visual_review_context(story, "b", "x")
        self.assertIn("Must show:hands typing", ctx)
        self.assertNotIn("smil", ctx)
        self.assertIn("coffee cup", ctx)


if __name__ == "__main__":
    unittest.main()
