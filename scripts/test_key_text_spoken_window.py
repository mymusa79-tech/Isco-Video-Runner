"""Key text must be on screen only while it is spoken, never twice, never off-time."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import podcast_key_text as kt  # noqa: E402

SECTIONS = [
    {"id": "s1", "narration": "A: لماذا لا أسأل فقط؟ B: لأن السؤال يكشف الخوف. وهذا طبيعي جدا."},
    {"id": "s2", "narration": "B: ربما تتحمل الخسارة المالية لكنك تخشى السؤال. ثم تقرر بهدوء تام."},
    {"id": "s3", "narration": "B: القرار الشجاع يبدأ بصدق مع النفس. لا يلزم أن ينتهي بنهاية سعيدة."},
]
TIMELINE = {
    "status": "pass",
    "section_events": [
        {"section_id": "s1", "start": 0.0, "end": 20.0},
        {"section_id": "s2", "start": 20.0, "end": 40.0},
        {"section_id": "s3", "start": 40.0, "end": 60.0},
    ],
    "identity_events": [],
}
CUES = [
    {"start": 1.0, "end": 3.0, "text": "لماذا لا أسأل فقط؟", "speaker": "A"},
    {"start": 4.0, "end": 8.0, "text": "لأن السؤال يكشف الخوف.", "speaker": "B"},
    {"start": 24.0, "end": 29.0, "text": "ربما تتحمل الخسارة المالية لكنك تخشى السؤال.", "speaker": "B"},
    {"start": 50.0, "end": 55.0, "text": "لا يلزم أن ينتهي بنهاية سعيدة.", "speaker": "B"},
]


class SpokenWindowTests(unittest.TestCase):
    def test_window_is_exactly_the_spoken_cue(self):
        got = kt.locate_spoken_window("لماذا لا أسأل فقط؟", CUES, near=(0, 20))
        self.assertEqual(got, (1.0, 3.0))

    def test_sentence_not_spoken_in_its_section_is_dropped(self):
        self.assertIsNone(kt.locate_spoken_window("لماذا لا أسأل فقط؟", CUES, near=(40, 60)))
        self.assertIsNone(kt.locate_spoken_window("جملة غير منطوقة أبدا", CUES, near=(0, 60)))

    def test_events_follow_speech_and_never_repeat(self):
        events = kt.build_events(script={"sections": SECTIONS}, timeline=TIMELINE, fmt="film", cues=CUES)
        self.assertTrue(events)
        texts = [e["text"] for e in events]
        self.assertEqual(len(texts), len(set(texts)))
        for event in events:
            cue = next(c for c in CUES if event["text"].rstrip("؟.") in c["text"])
            self.assertAlmostEqual(event["start"], cue["start"], places=1)
            self.assertAlmostEqual(event["end"], cue["end"], places=1)
            self.assertEqual(event["timing"], "spoken_window")

    def test_ass_is_navy_gold_card_with_text_on_top(self):
        ass = kt.build_ass([{"start": 1, "end": 4, "text": "لماذا لا أسأل فقط؟", "role": "hook"}], fmt="film")
        for name in (",shade,", ",card,", ",rim,", ",accent,", ",line,"):
            self.assertIn(name, ass)
        self.assertIn("&H26160E&", ass)
        self.assertIn("&H4BA2C9&", ass)
        card = next(l for l in ass.splitlines() if ",card," in l)
        nums = [int(n) for n in __import__("re").findall(r"-?\d+", card.split("\\p1}")[1].split("{")[0])]
        self.assertLessEqual(max(nums[1::2]), 980)

    def test_ass_renders_without_black_band(self):
        ass = kt.build_ass([{"start": 0, "end": 2, "text": "لماذا لا أسأل فقط؟", "role": "hook"}], fmt="film")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k.ass"
            path.write_text(ass, encoding="utf-8")
            png = Path(tmp) / "f.png"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=1920x1080:d=2",
                 "-vf", f"ass={path}", "-ss", "1", "-frames:v", "1", str(png)], check=True)
            self.assertTrue(png.stat().st_size > 0)


if __name__ == "__main__":
    unittest.main()
