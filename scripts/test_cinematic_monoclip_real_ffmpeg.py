"""Real offline FFmpeg smoke: one continuous pinned shot, exact voice end, no outro.

This exercises clean_v2.media.render_video on real synthetic video/audio, without
Pexels/Gemini credentials or paid providers. Synthetic colour bars are NOT content.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2.media import render_video, probe_duration, _require_cinematic_monoclip_whole_video


def run(*args):
    return subprocess.run(args, check=True, timeout=220, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "FFmpeg and FFprobe required for actual video verification")
class CinematicMonoclipActualRenderTests(unittest.TestCase):
    def _make_lavfi(self, dest, expression, seconds):
        run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", expression, "-t", str(seconds),
            "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", str(dest))

    def _fixture(self, root, style):
        srcs = [root / "hook.mp4", root / "main.mp4", root / "payoff.mp4"]
        self._make_lavfi(srcs[0], "color=c=yellow:size=270x480:rate=30", 1.3)
        self._make_lavfi(srcs[1], "color=c=red:size=270x480:rate=30,fade=t=in:st=0:d=0.4", 4.6)
        self._make_lavfi(srcs[2], "color=c=cyan:size=270x480:rate=30", 1.4)
        wav = root / "narration.wav"
        run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "4.1", "-c:a", "pcm_s16le", str(wav))
        (root / "production.json").write_text(json.dumps({
            "visual_style": style, "format": "short", "template": "micro_story",
        }), encoding="utf-8")
        manifest = {"assets": [
            {
                "local_file": src.name,
                "section_id": f"s{i+1}",
                "role": "hook" if i == 0 else "body",
                "source_actual": "owner_art" if i != 1 else "stock_video",
                "provider": "owner_art" if i != 1 else "pexels",
                **({"media_kind": "video", "stock_pinned": True} if i == 1 else {}),
                "owner_asset": f"assets/scene_0{i+1}.png",
                "owner_transition": "cut",
            }
            for i, src in enumerate(srcs)
        ]}
        (root / "rights-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        timeline = {
            "status": "pass", "template": "micro_story",
            "cinematic_monoclip": True, "voice_seconds_measured": 4.1,
            "identity_events": [
                {"kind": "hook", "start": 0, "end": 0.5},
                {"kind": "post_hook_silence", "start": 0.5, "end": 0.65},
                {"kind": "topic", "start": 0.65, "end": 4.1},
            ],
            "section_events": [
                {"section_id": "s1", "start": 0, "end": 0.65},
                {"section_id": "s2", "start": 0.65, "end": 3.15},
                {"section_id": "s3", "start": 3.15, "end": 4.1},
            ],
        }
        (root / "timeline-first.json").write_text(json.dumps(timeline), encoding="utf-8")
        return wav, srcs

    def _pixel(self, movie, when):
        data = run("ffmpeg", "-hide_banner", "-loglevel", "error",
                   "-ss", str(when), "-i", str(movie), "-frames:v", "1",
                   "-vf", "scale=1:1:flags=area,format=rgb24",
                   "-f", "rawvideo", "-").stdout
        if len(data) != 3:
            raise AssertionError(f"cannot decode pixel at {when}s")
        return list(data)

    def test_fail_closed_on_short_or_unverified_whole_stock(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            narration, sources = self._fixture(root, "color_cinematic")
            with mock.patch("clean_v2.media.probe_duration", return_value=3.5):
                with self.assertRaisesRegex(RuntimeError, "whole_stock_short_for_voice"):
                    _require_cinematic_monoclip_whole_video(root, sources, 4.1)
            manifest_path = root / "rights-manifest.json"
            doc = json.loads(manifest_path.read_text())
            doc["assets"][1]["stock_pinned"] = False
            manifest_path.write_text(json.dumps(doc))
            with self.assertRaisesRegex(RuntimeError, "pinned_video_provenance_missing"):
                _require_cinematic_monoclip_whole_video(root, sources, 4.1)

    def test_actual_both_color_and_bw_no_outro_end_to_end(self):
        for style in ("color_cinematic", "bw_cinematic"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                narration, sources = self._fixture(root, style)
                output = render_video(narration, sources, root / "final.mp4", fmt="short")
                self.assertTrue(output.is_file())
                self.assertGreater(output.stat().st_size, 2048)
                self.assertAlmostEqual(probe_duration(output), 4.1, delta=0.18)
                opening = self._pixel(output, 0.65)
                main = self._pixel(output, 1.70)
                ending = self._pixel(output, 3.80)
                if style == "bw_cinematic":
                    self.assertLessEqual(max(opening)-min(opening), 6, opening)
                    self.assertLessEqual(max(main)-min(main), 6, main)
                    self.assertLessEqual(max(ending)-min(ending), 6, ending)
                    self.assertLess(opening[0], 140, opening)
                    self.assertLess(ending[0], 140, ending)
                else:
                    self.assertGreater(opening[0], opening[1] * 1.4, opening)
                    self.assertGreater(main[0], main[1] * 1.4, main)
                    self.assertGreater(ending[0], ending[1] * 1.4, ending)
                    self.assertLess(ending[2], 80, ending)  # not a cyan still at payoff
                self.assertFalse((root / ".timeline-body.mp4").exists())
                evidence_directory = os.environ.get("MONOCLIP_QA_OUTPUT_DIR", "").strip()
                if evidence_directory:
                    destination = Path(evidence_directory)
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(output, destination / f"{style}_synthetic.mp4")
                    for at_seconds, name in ((1.70, "main"), (3.80, "end")):
                        run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", str(at_seconds), "-i", str(output),
                            "-frames:v", "1", "-vf", "scale=270:-2",
                            str(destination / f"{style}_{name}.png"))
                report = json.loads((root / "edit-decision-contract.json").read_text())
                self.assertEqual(len(report["slots"]), 1)  # same shot from hook to payoff
                self.assertFalse(any(slot.get("section_id") == "__identity__" and
                                     slot.get("start", 0) >= 3.15
                                     for slot in report["slots"]))


if __name__ == "__main__":
    unittest.main()
