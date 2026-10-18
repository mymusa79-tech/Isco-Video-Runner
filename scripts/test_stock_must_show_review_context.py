"""The owner's stock `must_show` must reach the visual review context (it was ignored before)."""
import unittest

from clean_v2 import ready_production
from scripts.test_production_package import manifest


class StockMustShowReachesReview(unittest.TestCase):
    def _beats(self, stock):
        doc = manifest("short")
        doc["scenes"][1]["stock"] = stock
        return {b["id"]: b for b in ready_production.build_story(doc)["beats"]}

    def test_must_show_is_part_of_review_intent(self):
        stock = {"query_en": "hands sorting paper documents", "must_show": "paper documents only; no money, no face"}
        beat = self._beats(stock)[manifest("short")["scenes"][1]["id"]]
        self.assertEqual(beat["stock_query_en"], "hands sorting paper documents")  # the search query is unchanged
        self.assertIn("no money", beat["shot_intent"])
        self.assertIn("paper documents only; no money, no face", beat["semantic_must_have"])

    def test_without_must_show_nothing_changes(self):
        stock = {"query_en": "hands sorting paper documents"}
        beat = self._beats(stock)[manifest("short")["scenes"][1]["id"]]
        self.assertEqual(beat["shot_intent"], "hands sorting paper documents")
        self.assertEqual(beat["semantic_must_have"], ["hands sorting paper documents"])


if __name__ == "__main__":
    unittest.main()
