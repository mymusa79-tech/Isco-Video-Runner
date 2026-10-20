from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.film_captions import (
    FilmCaptionError, build_ass, build_srt, apply_film_captions,
)
from clean_v2.podcast_captions import build_events
from clean_v2.timeline_render import render_identity_composition


class FilmCaptionsTests(unittest.TestCase):
    def test_film_caption_look_is_not_podcast_card(self) -> None:
        events = [
            {"start": 0.0, "end": 2.5, "text": "تفتح الهاتف بلا إشعار."},
            {"start": 2.5, "end": 5.0, "text": "لكن ما الذي تبحث عنه فعلًا؟"},
        ]
        ass = build_ass(events)
        self.assertIn("Style: Film,Cairo", ass)
        self.assertIn("\\fad(100,130)", ass)
        self.assertEqual(ass.count("Dialogue: 0,"), 2)
        self.assertNotIn("card,", ass)
        self.assertNotIn("shade,", ass)
        self.assertNotIn("\\p1", ass)
        self.assertIn("تفتح الهاتف", build_srt(events))

    def test_only_spoken_hook_and_topic_are_subtitled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "timeline-first.json").write_text(json.dumps({
                "status": "pass",
                "audio_units": [
                    {"section_id": "s1", "chunk": 1, "role": "hook",
                     "start": 0.0, "end": 3.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 2, "role": "prayer",
                     "start": 3.0, "end": 5.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 3, "role": "channel_identity",
                     "start": 5.0, "end": 8.0, "file": "missing.wav"},
                    {"section_id": "s1", "chunk": 4, "role": "topic",
                     "start": 8.0, "end": 12.0, "file": "missing.wav"},
                ],
            }), encoding="utf-8")
            (root / "voice-sections.json").write_text(json.dumps({
                "sections": [{"id": "s1", "chunks": [
                    {"chunk": 1, "text": "لماذا تمسك هاتفك رغم الصمت؟"},
                    {"chunk": 2, "text": "اللهم صل وسلم على نبينا محمد."},
                    {"chunk": 3, "text": "هنا نداء اليقظة."},
                    {"chunk": 4, "text": "العادة تبدأ قبل أن تسمع أي إشعار."},
                ]}],
            }, ensure_ascii=False), encoding="utf-8")
            events, mode = build_events(
                root, fmt="film", caption_roles=frozenset({"hook", "topic"})
            )
            self.assertGreaterEqual(len(events), 2)
            self.assertTrue(all(
                (float(e["end"]) <= 3.01 or float(e["start"]) >= 7.99)
                for e in events
            ))
            self.assertNotIn("اللهم", " ".join(e["text"] for e in events))
            self.assertNotIn("هنا نداء", " ".join(e["text"] for e in events))
            self.assertIn(mode, {
                "measured_audio_units_char_weighted",
                "measured_audio_units_snapped_to_pauses",
            })

    def test_film_render_does_not_draw_second_intro(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            assets = {kind: root / f"{kind}.mp4" for kind in ("intro", "prayer", "outro")}
            for path in assets.values():
                path.write_bytes(b"asset" * 300)
            timeline = {
                "voice_seconds_measured": 23.0,
                "identity_events": [
                    {"kind": "hook", "start": 0.0, "end": 3.0},
                    {"kind": "prayer", "start": 3.35, "end": 5.3},
                    {"kind": "post_prayer_silence", "start": 5.3, "end": 5.75},
                    {"kind": "channel_identity", "start": 5.75, "end": 8.3},
                    {"kind": "pre_topic_silence", "start": 8.3, "end": 8.65},
                    {"kind": "topic", "start": 8.65, "end": 19.5},
                    {"kind": "outro", "start": 19.5, "end": 23.0},
                    {"kind": "final_silence", "start": 19.5, "end": 23.0},
                ],
            }
            with mock.patch("clean_v2.timeline_render.identity_asset_paths", return_value=assets), \\
                 mock.patch("clean_v2.timeline_render.subprocess.run") as runner:
                render_identity_composition(root / "source.mp4", root / "final.mp4",
                                            fmt="film", timeline=timeline)
            cmd = runner.call_args.args[0]
            graph = cmd[cmd.index("-filter_complex") + 1]
            self.assertIn("[0:v]null[v1]", graph)
            self.assertIn("[channel]", graph)
            self.assertNotIn("[intro]", graph)

if __name__ == "__main__":
    unittest.main()
