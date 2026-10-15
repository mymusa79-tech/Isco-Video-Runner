from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import podcast_key_text as key_text

SECTIONS = [
    {"id": "s1", "narration": "A: لماذا نشعر بالتعب رغم أننا لم نفعل شيئًا؟ اللهم صل وسلم على نبينا محمد. B: لأن القرارات الصغيرة تستهلك طاقتك كل يوم. المشكلة ليست في الجهد بل في كثرة الخيارات التي تواجهها."},
    {"id": "s2", "narration": "A: وما الحل؟ B: الحقيقة أن تقلل القرارات لا أن تزيد الجهد. لهذا ابدأ بقرار واحد ثابت كل صباح، ثم راقب كيف تتغير طاقتك."},
    {"id": "s3", "narration": "A: وهل ينجح هذا مع الجميع؟ B: لا يوجد حل واحد، لكن المبدأ واحد: كل قرار تحذفه يعيد إليك جزءًا من انتباهك."},
]


def _make(root: Path) -> None:
    events, cursor = [], 0.0
    for section in SECTIONS:
        events.append({"section_id": section["id"], "start": cursor, "end": cursor + 70, "duration_seconds": 70})
        cursor += 70
    (root / "timeline-first.json").write_text(json.dumps({
        "status": "pass", "section_events": events, "audio_units": [],
        "identity_events": [
            {"kind": "hook", "start": 0.0, "end": 4.0},
            {"kind": "prayer", "start": 10.0, "end": 14.0},
            {"kind": "topic", "start": 16.0, "end": cursor - 8},
        ],
    }), encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"color=c=0x1b2a3a:s=1920x1080:r=2:d={cursor}", "-pix_fmt", "yuv420p", str(root / "final.mp4")],
        check=True,
    )


class KeyTextTraceTests(unittest.TestCase):
    def test_podcast_and_film_key_text_is_traced_to_the_script_and_font_is_reported(self) -> None:
        for fmt, apply in (("podcast", key_text.apply_podcast_key_text), ("film", key_text.apply_film_key_text)):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _make(root)
                report = apply(output_dir=root, final_path=root / "final.mp4", script={"sections": SECTIONS})
                self.assertEqual(report["status"], "pass")
                haystack = " ".join(s["narration"] for s in SECTIONS)
                from clean_v2.short_timed_text import occurs_in_source

                self.assertTrue(report["events"])
                for event in report["events"]:
                    self.assertTrue(occurs_in_source(event["text"], haystack), event["text"])
                self.assertEqual(report["font_actually_used"]["requested"], "Cairo")

    def test_key_text_that_is_not_in_the_approved_script_is_never_drawn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make(root)
            real = key_text.build_events

            def tampered(**kwargs):
                rows = real(**kwargs)
                rows[0] = {**rows[0], "text": "جملة لم تكتب في النص"}
                return rows

            with mock.patch.object(key_text, "build_events", tampered):
                with self.assertRaisesRegex(key_text.PodcastKeyTextError, "not_in_approved_script"):
                    key_text.apply_podcast_key_text(output_dir=root, final_path=root / "final.mp4", script={"sections": SECTIONS})


if __name__ == "__main__":
    unittest.main()
