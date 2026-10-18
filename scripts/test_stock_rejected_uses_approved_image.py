from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import visual_qa  # noqa: E402
from clean_v2.owner_art import ART_DIR_ENV  # noqa: E402
from clean_v2.simple_mode import simple_visual_qa  # noqa: E402


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class RejectedStockUsesApprovedImageTests(unittest.TestCase):
    def _setup(self, root: Path, *, with_art: bool):
        (root / "visuals").mkdir()
        art = root / "art"
        (art / "assets").mkdir(parents=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=orange:s=540x960:d=1",
                        "-frames:v", "1", str(art / "assets" / "scene_04.jpg")], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=270x480:d=2",
                        "-pix_fmt", "yuv420p", str(root / "visuals" / "stock.mp4")], check=True)
        return art if with_art else None

    def _run(self, row, art):
        env = {ART_DIR_ENV: str(art)} if art else {}
        with mock.patch.dict(os.environ, env), mock.patch.object(
                visual_qa, "_apply_observed_visual_proof", lambda a: {"observed_proof_status": "not_matched"}):
            if not art:
                os.environ.pop(ART_DIR_ENV, None)
            return simple_visual_qa(output_dir=self.root, plan={}, script={}, rights=[row], fmt="short")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _row(self, **extra):
        return {"provider": "pexels", "asset_id": "9", "local_file": "stock.mp4", "beat_id": "b4",
                "human_choice": "auto", **extra}

    def test_rejected_stock_becomes_the_approved_image(self):
        art = self._setup(self.root, with_art=True)
        row = self._row(stock_requested=True, approved_fallback_asset="assets/scene_04.jpg")
        report = self._run(row, art)
        self.assertEqual(row["provider"], "owner_art")
        self.assertEqual(row["visual_verification"], "owner_designed_art")
        self.assertEqual(row["rejected_source"]["provider"], "pexels")
        self.assertEqual(report["fallback_backgrounds"], 0)
        self.assertTrue((self.root / "visuals" / "stock.mp4").stat().st_size > 0)

    def test_plain_stock_row_keeps_the_old_local_replacement(self):
        self._setup(self.root, with_art=True)
        row = self._row()
        report = self._run(row, None)
        self.assertEqual(row["provider"], "generated_local")
        self.assertEqual(report["fallback_backgrounds"], 1)

    def test_missing_approved_image_does_not_crash(self):
        art = self._setup(self.root, with_art=True)
        row = self._row(stock_requested=True, approved_fallback_asset="assets/missing.jpg")
        report = self._run(row, art)
        self.assertEqual(row["provider"], "generated_local")
        self.assertEqual(report["fallback_backgrounds"], 1)


if __name__ == "__main__":
    unittest.main()
