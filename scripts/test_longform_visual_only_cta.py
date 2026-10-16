from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import visual_cta


def _timeline(root: Path, *, topic=(18.24, 751.68)) -> None:
    (root / "timeline-first.json").write_text(json.dumps({
        "identity_events": [
            {"kind": "hook", "start": 0.0, "end": 5.0},
            {"kind": "topic", "start": topic[0], "end": topic[1]},
            {"kind": "outro", "start": topic[1], "end": topic[1] + 6.5},
        ]}), encoding="utf-8")
    (root / "narration.txt").write_text("نص الحلقة المعتمد", encoding="utf-8")


class SelectionTests(unittest.TestCase):
    def test_three_spread_deterministic_events_outside_the_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _timeline(root)
            first = visual_cta._longform_visual_only_events(output_dir=root, duration=758.18, fmt="podcast")
            second = visual_cta._longform_visual_only_events(output_dir=root, duration=758.18, fmt="podcast")
            self.assertEqual(first, second)
            self.assertEqual(len(first), 3)
            self.assertEqual(len({e.mode for e in first}), 3)
            for event in first:
                self.assertGreaterEqual(event.start_seconds, 30.5)
                self.assertLessEqual(event.end_seconds, 758.18 - 12.0)
            starts = [e.start_seconds for e in first]
            self.assertEqual(starts, sorted(starts))
            self.assertGreater(starts[1] - starts[0], 60)

    def test_short_topic_gets_two_and_tiny_topic_gets_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _timeline(root, topic=(18.0, 200.0))
            self.assertEqual(len(visual_cta._longform_visual_only_events(output_dir=root, duration=206.5, fmt="film")), 2)
            _timeline(root, topic=(18.0, 80.0))
            self.assertEqual(visual_cta._longform_visual_only_events(output_dir=root, duration=86.5, fmt="film"), [])


class RenderTests(unittest.TestCase):
    def test_zip_podcast_gets_silent_reminders_and_untouched_dialogue(self) -> None:
        def mean_db(path: Path) -> float:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect",
                                  "-f", "null", "-"], capture_output=True, text=True).stderr
            return float(out.split("mean_volume:")[1].split("dB")[0])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _timeline(root, topic=(18.0, 140.0))
            (root / "cta-plan.json").write_text(json.dumps({"mode": "none", "spoken_text": ""}), encoding="utf-8")
            video, narration = root / "final.mp4", root / "narration.wav"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=300:d=146",
                            "-c:a", "pcm_s16le", str(narration)], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x202830:s=1920x1080:r=5:d=146",
                            "-i", str(narration), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-shortest", str(video)], check=True)
            before = mean_db(video)

            def fake_combo(destination: Path, *, fmt: str) -> Path:
                from PIL import Image
                Image.new("RGBA", (560, 198), (200, 40, 40, 255)).save(destination)
                return destination

            with mock.patch.object(visual_cta, "_render_arabic_subscribe_combo", side_effect=fake_combo):
                report = visual_cta.apply_visual_cta_assets(
                    output_dir=root, final_path=video, narration_path=narration, script={}, fmt="podcast")
            self.assertEqual(report["status"], "applied")
            self.assertTrue(report["silent_visual_only"])
            self.assertGreaterEqual(report["event_count"], 2)
            self.assertLessEqual(report["event_count"], 3)
            self.assertAlmostEqual(mean_db(video), before, delta=0.6)  # no click, no mix change


if __name__ == "__main__":
    unittest.main()
