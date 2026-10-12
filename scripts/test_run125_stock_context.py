from __future__ import annotations

import unittest
from unittest import mock

from clean_v2 import media


class Run125StockContextTests(unittest.TestCase):
    """Run 125: metadata should rule out unrelated scenes before download."""

    def test_medical_calendar_is_off_topic_for_productivity_but_valid_for_health(self) -> None:
        context = "Calendar appointment for vaccination with syringe and medicine pills"
        self.assertTrue(
            media._stock_metadata_context_conflict(
                "calendar marked date on a desk for tomorrow work", context
            )
        )
        self.assertFalse(
            media._stock_metadata_context_conflict(
                "vaccination appointment on clinic calendar", context
            )
        )

    def test_shopping_list_is_not_a_productivity_checklist(self) -> None:
        self.assertTrue(
            media._stock_metadata_context_conflict(
                "unfinished to do task checklist on a table",
                "Handwritten grocery shopping list with milk and eggs",
            )
        )
        self.assertFalse(
            media._stock_metadata_context_conflict(
                "shopping grocery list for the family",
                "Handwritten grocery shopping list with milk and eggs",
            )
        )

    def test_spinning_toy_is_not_an_action_with_a_key(self) -> None:
        self.assertTrue(
            media._stock_metadata_context_conflict(
                "single key on wooden table",
                "Spinning top toy on wooden table",
            )
        )
        self.assertFalse(
            media._stock_metadata_context_conflict(
                "key on table", "Metal house key placed on wood"
            )
        )

    def test_explicit_hands_only_avoids_obvious_face_metadata(self) -> None:
        self.assertTrue(
            media._stock_metadata_context_conflict(
                "hands only writing a to do list no face visible",
                "Woman gazing out a window portrait",
            )
        )
        self.assertFalse(
            media._stock_metadata_context_conflict(
                "hands only writing a to do list no face visible",
                "Hands writing notes on desk",
            )
        )

    def test_missing_metadata_keeps_existing_provider_fallback(self) -> None:
        self.assertFalse(media._stock_metadata_context_conflict("calendar for tomorrow", ""))
        self.assertFalse(media._stock_metadata_context_conflict("", "vaccination calendar"))

    def test_primary_pexels_selection_skips_off_topic_photo_without_another_request(self) -> None:
        photos = [
            {
                "id": 10, "width": 1080, "height": 1920,
                "url": "https://www.pexels.com/photo/vaccination-calendar-with-pills-10/",
                "alt": "Calendar for vaccination appointment with syringe and pills",
                "src": {"large": "https://images.pexels.com/photos/10/image.jpg"},
            },
            {
                "id": 11, "width": 1080, "height": 1920,
                "url": "https://www.pexels.com/photo/marked-day-on-a-desk-calendar-11/",
                "alt": "A simple desk calendar with a date marked",
                "src": {"large": "https://images.pexels.com/photos/11/image.jpg"},
            },
        ]
        with (
            mock.patch.object(media, "_read_secret", return_value="test-key"),
            mock.patch.object(media, "_get_json", return_value={"photos": photos}) as fetch,
        ):
            selected = media.StockVisualSource()._pexels_photo(
                "calendar with day marked for tomorrow work", portrait=True
            )
        self.assertIsNotNone(selected)
        self.assertEqual(selected["asset_id"], "11")
        fetch.assert_called_once()

    def test_semantic_score_does_not_claim_image_validation(self) -> None:
        self.assertFalse(
            media._stock_metadata_context_conflict(
                "empty desk with lamp", "Wooden desk with small lamp"
            )
        )


if __name__ == "__main__":
    unittest.main()
