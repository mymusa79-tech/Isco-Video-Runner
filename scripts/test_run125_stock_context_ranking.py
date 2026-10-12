from __future__ import annotations

import unittest
from unittest import mock

from clean_v2 import media


class Run125StockContextRankingTests(unittest.TestCase):
    def test_run125_off_topic_metadata_is_excluded(self) -> None:
        cases = (
            (
                "hand turning calendar page to reveal marked date",
                "A calendar marked with a vaccination date, syringe, and medicine vials.",
            ),
            (
                "close up of long unfinished to do list on paper",
                "Close-up of a hand writing a grocery list with a marker on brown paper.",
            ),
            (
                "hand placing a single key on wooden table",
                "Spinning top on a wooden table in soft lighting.",
            ),
            (
                "hands only close-up of a writing task",
                "A thoughtful woman leans on a kitchen counter, gazing out a window.",
            ),
        )
        for query, metadata in cases:
            with self.subTest(query=query):
                self.assertTrue(media._stock_metadata_context_conflict(query, metadata))

    def test_intentionally_medical_or_grocery_footage_remains_available(self) -> None:
        self.assertFalse(media._stock_metadata_context_conflict(
            "calendar with vaccination date for health awareness",
            "A calendar marked with a vaccination date, syringe, and medicine vials.",
        ))
        self.assertFalse(media._stock_metadata_context_conflict(
            "hand writing a grocery shopping list",
            "Close-up of a hand writing a grocery list with a marker.",
        ))
        self.assertFalse(media._stock_metadata_context_conflict(
            "spinning top toy on wooden table",
            "Spinning top on a wooden table.",
        ))

    def test_unknown_metadata_is_not_an_automatic_block(self) -> None:
        self.assertFalse(media._stock_metadata_context_conflict("hand turning a calendar page", ""))
        self.assertFalse(media._stock_metadata_context_conflict("", "A calendar vaccination date"))
        self.assertFalse(media._stock_metadata_context_conflict(
            "person studying at desk", "Desk lamp and open notebook",
        ))

    def test_primary_photo_search_uses_next_existing_result(self) -> None:
        photos = [
            {
                "id": 8830699,
                "alt": "A calendar marked with a vaccination date, syringe, and medicine vials.",
                "url": "https://www.pexels.com/photo/hand-writing-a-circle-on-the-calendar-8830699/",
                "src": {"large": "https://images.pexels.com/medical.jpg"},
                "width": 1080, "height": 1920,
            },
            {
                "id": 42,
                "alt": "Hand turning the paper calendar to a new date.",
                "url": "https://www.pexels.com/photo/calendar-42/",
                "src": {"large": "https://images.pexels.com/calendar.jpg"},
                "width": 1080, "height": 1920,
            },
        ]
        stock = media.StockVisualSource()
        with mock.patch.object(media, "_read_secret", return_value="test_key"), mock.patch.object(
            media, "_get_json", return_value={"photos": photos},
        ):
            selected = stock._pexels_photo(
                "hand turning calendar page to reveal marked date", portrait=True,
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["asset_id"], "42")
        self.assertEqual(selected["provider"], "pexels")
        self.assertEqual(len([event for event in stock.events if event.get("wire_attempted")]), 1)


if __name__ == "__main__":
    unittest.main()