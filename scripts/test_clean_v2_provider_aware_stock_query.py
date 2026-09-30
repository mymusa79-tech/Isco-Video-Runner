from __future__ import annotations

import unittest
from unittest import mock

from clean_v2 import media as media_module


SCENE_QUERY = (
    "cinematic close up exhausted office worker checking work messages "
    "late at night realistic lighting"
)


class CleanV2ProviderAwareStockQueryTests(unittest.TestCase):
    def test_same_scene_gets_provider_specific_search_shape_without_semantic_drift(self) -> None:
        pexels = media_module._provider_stock_query(SCENE_QUERY, "pexels")
        coverr = media_module._provider_stock_query(SCENE_QUERY, "coverr")
        pixabay = media_module._provider_stock_query(SCENE_QUERY, "pixabay")

        self.assertEqual(
            pexels,
            "close up exhausted office worker checking work messages late at night realistic lighting",
        )
        self.assertEqual(
            coverr,
            "close up exhausted office worker checking work messages late at night",
        )
        self.assertEqual(
            pixabay,
            "exhausted office worker checking work messages late night",
        )
        self.assertEqual(len({pexels, coverr, pixabay}), 3)

        for query in (pexels, coverr, pixabay):
            self.assertIn("office", query)
            self.assertIn("worker", query)
            self.assertIn("messages", query)
            self.assertIn("late", query)
            self.assertIn("night", query)

        self.assertLessEqual(len(pexels), 200)
        self.assertLessEqual(len(coverr), 160)
        self.assertLessEqual(len(pixabay), 100)

    def test_provider_entrypoints_adapt_query_before_any_wire_attempt(self) -> None:
        source = media_module.StockVisualSource()
        expected = {
            "coverr": media_module._provider_stock_query(SCENE_QUERY, "coverr"),
            "pexels": media_module._provider_stock_query(SCENE_QUERY, "pexels"),
            "pexels_photo": media_module._provider_stock_query(SCENE_QUERY, "pexels_photo"),
            "pixabay": media_module._provider_stock_query(SCENE_QUERY, "pixabay"),
            "pixabay_photo": media_module._provider_stock_query(SCENE_QUERY, "pixabay_photo"),
        }

        with mock.patch.object(media_module, "_read_secret", return_value=""):
            self.assertIsNone(source._coverr(SCENE_QUERY, portrait=True))
            self.assertIsNone(source._pexels(SCENE_QUERY, portrait=True))
            self.assertIsNone(source._pixabay(SCENE_QUERY, portrait=True))
            self.assertIsNone(source._pexels_photo(SCENE_QUERY, portrait=True))
            self.assertIsNone(source._pixabay_photo(SCENE_QUERY, portrait=True))
            self.assertEqual(
                source._pexels_recovery_pool(
                    SCENE_QUERY,
                    portrait=True,
                    limit=1,
                ),
                [],
            )
            self.assertEqual(
                source._pixabay_recovery_pool(
                    SCENE_QUERY,
                    portrait=True,
                    limit=1,
                ),
                [],
            )

        self.assertEqual(len(source.events), 7)
        for event in source.events:
            self.assertEqual(event["query"], expected[event["provider"]])
            self.assertFalse(event["wire_attempted"])


if __name__ == "__main__":
    unittest.main()
