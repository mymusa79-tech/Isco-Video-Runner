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


class AlternateDriftTests(unittest.TestCase):
    def test_run120_symbol_swap_is_rejected_early(self):
        from clean_v2 import visual_qa
        original = "hands holding a completed puzzle piece fitting perfectly into place"
        with self.assertRaises(visual_qa.AlternateQueryError) as raised:
            visual_qa._validate_alternate_query(
                {"alternate_query": "hands inserting a final key into a vintage wooden box lock"},
                original_query=original,
            )
        self.assertEqual(raised.exception.code, "alternate_query_drifted")
        ok = visual_qa._validate_alternate_query(
            {"alternate_query": "hands assembling final puzzle pieces on a table"},
            original_query=original,
        )
        self.assertTrue(ok["alternate_query"])


if __name__ == "__main__":
    unittest.main()
