from __future__ import annotations

import unittest
from types import SimpleNamespace

from clean_v2.identity_sequence import (
    PRAYER_SENTENCE,
    SHORT_CHANNEL_DEFINITION,
    inject_spoken_identity,
)
from clean_v2.visual_cta import _events


class ApprovedIdentityLiteTests(unittest.TestCase):
    def test_short_spoken_order_is_hook_prayer_definition_topic(self) -> None:
        sections = [
            {
                "id": "s1",
                "narration": (
                    "قد لا تكون المشكلة في الدافع نفسه. "
                    "حين تتوقف قليلًا ترى ما يحدث بوضوح."
                ),
            },
            {
                "id": "s2",
                "narration": "السبب العملي يبدأ حين تربط الحركة بالشعور.",
            },
            {
                "id": "s3",
                "narration": "ابدأ بخطوة واحدة واضحة الآن.",
            },
        ]
        inject_spoken_identity(sections, fmt="short")
        first = sections[0]["narration"]
        self.assertTrue(first.startswith("قد لا تكون المشكلة في الدافع نفسه."))
        self.assertEqual(first.count(PRAYER_SENTENCE), 1)
        self.assertEqual(first.count(SHORT_CHANNEL_DEFINITION), 1)
        self.assertLess(first.index(PRAYER_SENTENCE), first.index(SHORT_CHANNEL_DEFINITION))
        self.assertIn("حين تتوقف قليلًا", first)

    def test_short_visual_cta_never_exceeds_two(self) -> None:
        script = {"title": "كيف تنهض عندما تفقد الدافع؟"}
        events = _events(
            fmt="short",
            duration=36.0,
            script=script,
            authored_mode="none",
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].mode, "comment")
        self.assertEqual(events[1].mode, "subscribe_combo")
        self.assertGreaterEqual(events[0].start_seconds, 7.0)
        self.assertLess(events[-1].end_seconds, 36.0)

    def test_shorter_short_uses_only_one_cta(self) -> None:
        events = _events(
            fmt="short",
            duration=24.0,
            script={"title": "فكرة عملية"},
            authored_mode="none",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].mode, "like")

    def test_long_cta_count_scales_sparsely_with_runtime(self) -> None:
        script = {"title": "حلقة طويلة"}
        short_long = _events(
            fmt="film",
            duration=150.0,
            script=script,
            authored_mode="comment",
        )
        medium_long = _events(
            fmt="film",
            duration=300.0,
            script=script,
            authored_mode="comment",
        )
        very_long = _events(
            fmt="film",
            duration=600.0,
            script=script,
            authored_mode="comment",
        )
        self.assertLessEqual(len(short_long), 2)
        self.assertLessEqual(len(medium_long), 3)
        self.assertLessEqual(len(very_long), 4)
        for event in very_long:
            self.assertLessEqual(event.end_seconds, 600.0 - 10.0)


if __name__ == "__main__":
    unittest.main()
