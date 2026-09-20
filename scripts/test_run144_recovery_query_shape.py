from __future__ import annotations

import unittest

from clean_v2.visual_qa import (
    _alternate_visual_query_prompt,
    _validate_alternate_query,
)


ORIGINAL = "hand holding a calendar and a coffee mug on a desk"

RUN144_ATTEMPT1 = (
    "person sitting at a cluttered desk looking frustrated at a calendar with "
    "crossed-out dates, holding an untouched coffee mug, comparing it to a "
    "neatly organized planner"
)

RUN144_ATTEMPT2 = (
    "person sitting at a cluttered desk looking frustrated while holding a "
    "half-empty coffee mug and a calendar with crossed-out dates, comparing "
    "it to a neatly organized planner"
)


class Run144RecoveryQueryShapeTests(unittest.TestCase):
    def test_prompt_requires_one_short_searchable_scene(self) -> None:
        prompt = _alternate_visual_query_prompt(
            original_query=ORIGINAL,
            narration_context="خطط إدارة الوقت تنهار عندما لا تناسب الواقع اليومي.",
        )
        self.assertIn("4 to 10 English words only", prompt)
        self.assertIn("ONE observable action or ONE simple setting", prompt)
        self.assertIn("Do not use comparisons", prompt)

    def test_attempt1_verbose_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_alternate_query(
                {"alternate_query": RUN144_ATTEMPT1},
                original_query=ORIGINAL,
            )

    def test_attempt2_verbose_query_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _validate_alternate_query(
                {"alternate_query": RUN144_ATTEMPT2},
                original_query=ORIGINAL,
            )

    def test_concise_single_scene_query_is_admitted(self) -> None:
        value = "person checking calendar at cluttered desk"
        self.assertEqual(
            _validate_alternate_query(
                {"alternate_query": value},
                original_query=ORIGINAL,
            ),
            {"alternate_query": value},
        )

    def test_original_query_still_cannot_be_reused(self) -> None:
        with self.assertRaises(ValueError):
            _validate_alternate_query(
                {"alternate_query": ORIGINAL},
                original_query=ORIGINAL,
            )


if __name__ == "__main__":
    unittest.main()
