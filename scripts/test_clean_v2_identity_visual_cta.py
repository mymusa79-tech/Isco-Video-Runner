from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from clean_v2.identity_sequence import (
    PODCAST_CHANNEL_DEFINITION,
    PRAYER_SENTENCE,
    SHORT_CHANNEL_DEFINITION,
    assert_spoken_identity,
    identity_timing_profile,
    inject_spoken_identity,
)
from clean_v2 import timeline_render
from clean_v2.visual_cta import _events, _longform_contextual_event


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
        self.assertTrue(SHORT_CHANNEL_DEFINITION.startswith("هنا نداء اليقظة"))
        self.assertIn(
            f"{PRAYER_SENTENCE} {SHORT_CHANNEL_DEFINITION} حين تتوقف قليلًا",
            first,
        )

    def test_short_spoken_order_normalizes_punctuationless_hook_boundary(self) -> None:
        sections = [
            {"id": "s1", "narration": "قد لا تكون المشكلة في الوقت"},
            {"id": "s2", "narration": "ابدأ بخطوة واحدة واضحة الآن."},
            {"id": "s3", "narration": "ثم راقب ما يتغير."},
        ]
        inject_spoken_identity(sections, fmt="short")
        assert_spoken_identity(sections, fmt="short")

        first = sections[0]["narration"]
        self.assertTrue(first.startswith("قد لا تكون المشكلة في الوقت. "))
        self.assertIn(f"{PRAYER_SENTENCE} {SHORT_CHANNEL_DEFINITION}", first)
        self.assertEqual(first.count(PRAYER_SENTENCE), 1)
        self.assertEqual(first.count(SHORT_CHANNEL_DEFINITION), 1)

    def test_podcast_spoken_order_is_A_question_then_prayer_then_B_answer(self) -> None:
        sections = [
            {
                "id": "s1",
                "narration": (
                    "A: لماذا أعرف ما أريد ومع ذلك لا أتحرك؟ "
                    "B: لأن وضوح الهدف لا يلغي احتكاك البداية."
                ),
            },
            {
                "id": "s2",
                "narration": "B: هنا يبدأ الفرق حين تصبح أول حركة قابلة للتنفيذ.",
            },
        ]
        inject_spoken_identity(sections, fmt="podcast", opener="ignored dynamic opener")
        first = sections[0]["narration"]
        self.assertTrue(first.startswith("A: لماذا أعرف ما أريد"))
        self.assertEqual(first.count(PRAYER_SENTENCE), 1)
        self.assertEqual(first.count(PODCAST_CHANNEL_DEFINITION), 0)
        self.assertIn(f"{PRAYER_SENTENCE} B: لأن وضوح الهدف", first)
        assert_spoken_identity(sections, fmt="podcast", opener="ignored dynamic opener")

        timing = identity_timing_profile("podcast")
        self.assertEqual(timing["post_hook_silence_seconds"], 0.75)
        self.assertEqual(timing["intro_silence_seconds"], 6.00)
        self.assertEqual(timing["post_prayer_silence_seconds"], 0.65)
        self.assertEqual(timing["pre_topic_silence_seconds"], 0.00)
        self.assertEqual(timing["final_silence_seconds"], 6.50)

    def test_podcast_v8_identity_uses_asset_sfx_without_second_brand_card(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {kind: root / f"{kind}.mp4" for kind in ("intro", "prayer", "outro")}
            for path in assets.values():
                path.write_bytes(b"fixture" * 300)
            timeline = {"voice_seconds_measured": 30.0, "identity_events": [
                {"kind": "intro", "start": 5.0, "end": 11.0},
                {"kind": "prayer", "start": 11.0, "end": 13.0},
                {"kind": "post_prayer_silence", "start": 13.0, "end": 13.65},
                {"kind": "outro", "start": 23.5, "end": 25.0},
                {"kind": "final_silence", "start": 25.0, "end": 30.0},
            ]}
            with mock.patch.object(timeline_render, "identity_asset_paths", return_value=assets), \
                 mock.patch.object(timeline_render.subprocess, "run") as run:
                timeline_render.render_identity_composition(root / "source.mp4", root / "final.mp4",
                                                           fmt="podcast", timeline=timeline)
            command = run.call_args.args[0]
            filters = command[command.index("-filter_complex") + 1]
            self.assertNotIn("[channel]", filters)
            self.assertNotIn("[identity]", filters)
            self.assertIn("[v2][outro]overlay=0:0", filters)
            self.assertIn("[1:a]atrim=duration=6.000", filters)
            self.assertIn("[3:a]atrim", filters)
            self.assertIn("[0:a][aintro][aoutro]amix", filters)
            self.assertIn("between(t,11.000,13.650)", filters)
            self.assertIn("[aout]", command)
            self.assertEqual(command[command.index("-t") + 1], "30.000")

    def test_short_keeps_one_visual_only_social_cta(self) -> None:
        cases = (
            (36.0, "كيف تنهض عندما تفقد الدافع؟", "comment"),
            (24.0, "فكرة عملية", "like"),
        )
        for duration, title, expected_mode in cases:
            with self.subTest(duration=duration):
                events = _events(
                    fmt="short",
                    duration=duration,
                    script={"title": title},
                    authored_mode="none",
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].mode, expected_mode)
                self.assertLess(events[0].x, 540)

    def test_longform_fallback_uses_one_authored_action_only(self) -> None:
        for fmt in ("film", "podcast"):
            with self.subTest(fmt=fmt):
                events = _events(
                    fmt=fmt,
                    duration=240.0,
                    script={"title": "حلقة"},
                    authored_mode="comment",
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].mode, "comment")

    def test_longform_visual_cta_matches_spoken_schedule_and_mode(self) -> None:
        cases = (
            ("film", "comment", "comment"),
            ("podcast", "subscribe", "subscribe_combo"),
        )
        for fmt, authored, expected in cases:
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "cta-plan.json").write_text(
                    json.dumps(
                        {
                            "mode": authored,
                            "spoken_text": "CTA منطوقة",
                            "visual_only": False,
                            "schedule": {
                                "start_seconds": 72.0,
                                "end_seconds": 75.6,
                                "anchor_section_id": "s3",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                events = _longform_contextual_event(
                    output_dir=root,
                    fmt=fmt,
                    duration=180.0,
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].mode, expected)
                self.assertEqual(events[0].start_seconds, 72.0)
                self.assertLess(events[0].x, 960)

    def test_long_cta_count_is_exactly_one_when_authored(self) -> None:
        script = {"title": "حلقة طويلة"}
        for duration in (150.0, 300.0, 600.0):
            with self.subTest(duration=duration):
                events = _events(
                    fmt="film",
                    duration=duration,
                    script=script,
                    authored_mode="comment",
                )
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].mode, "comment")
                self.assertLessEqual(events[0].end_seconds, duration - 10.0)


if __name__ == "__main__":
    unittest.main()
