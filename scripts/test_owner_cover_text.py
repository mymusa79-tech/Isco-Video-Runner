from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from clean_v2 import owner_art


def _doc(asset, text):
    return {"format": "short", "scenes": [{"asset": "assets/scene_01.png"}], "cover": {"asset": asset, "text": text}}


class OwnerCoverTextTests(unittest.TestCase):
    def _run(self, doc, report=None):
        with tempfile.TemporaryDirectory() as root:
            art = Path(root) / "art"
            (art / "assets").mkdir(parents=True)
            Image.new("RGB", (864, 1536), (120, 90, 60)).save(art / "assets/scene_01.png")
            if doc["cover"]["asset"]:
                Image.new("RGB", (864, 1536), (10, 200, 10)).save(art / doc["cover"]["asset"])
            (art / "production.json").write_text(json.dumps(doc), encoding="utf-8")
            cover = Path(root) / "cover.jpg"
            Image.new("RGB", (1080, 1920), (23, 32, 51)).save(cover)
            before = cover.read_bytes()
            old = owner_art.os.environ.get(owner_art.ART_DIR_ENV)
            owner_art.os.environ[owner_art.ART_DIR_ENV] = str(art)
            try:
                out = owner_art.apply_owner_cover(cover, dict(report or {"status": "pass"}))
            finally:
                if old is None:
                    owner_art.os.environ.pop(owner_art.ART_DIR_ENV, None)
                else:
                    owner_art.os.environ[owner_art.ART_DIR_ENV] = old
            return out, before, cover.read_bytes(), Image.open(cover).size

    def test_no_cover_asset_draws_the_approved_text_on_the_first_scene(self) -> None:
        out, before, after, size = self._run(_doc(None, "لم يكن المال أهم شيء"))
        self.assertEqual(out["selection"], "approved_first_scene_with_cover_text")
        self.assertEqual(out["cover_text"], "لم يكن المال أهم شيء")
        self.assertEqual(out["provider_calls_added"], 0)
        self.assertNotEqual(before, after)
        self.assertEqual(size, (1080, 1920))

    def test_explicit_cover_asset_keeps_full_resolution(self) -> None:
        out, _before, _after, size = self._run(_doc("assets/cover_base.png", "ignored"))
        self.assertEqual(out["source"], "owner_cover_image")
        self.assertNotIn("selection", out)
        self.assertEqual(size, (1080, 1920))

    def test_no_text_keeps_the_generated_cover(self) -> None:
        out, before, after, _ = self._run(_doc(None, ""))
        self.assertEqual(before, after)
        self.assertNotIn("selection", out)


if __name__ == "__main__":
    unittest.main()
