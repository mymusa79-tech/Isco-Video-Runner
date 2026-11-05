"""Offline Monoclip resource archive and approved voice-routing checks.

All checks use local images or mocks. No Gemini/stock/network request.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from clean_v2.media import (
    GEMINI38_NIDAA_SHORT_PRIMARY_VOICE,
    GEMINI38_NIDAA_SHORT_PROVIDER,
    GEMINI38_LITE_TTS_MODEL,
    GeminiOnlyVoiceSynthesizer,
)
from clean_v2.pipeline import _voice_performance_mode_for_brief
from clean_v2.production_package import inspect_archive
from scripts.prepare_cinematic_monoclip_zip import prepare_archive
from scripts.test_production_package import manifest, archive_bytes

PINNED_STOCK = "https://www.pexels.com/video/a-man-walking-down-a-path-in-the-woods-28464399/"


def write_fixture(root: Path, style: str = "color_cinematic") -> Path:
    doc = manifest()
    doc["visual_style"] = style
    doc["scenes"][1]["stock"] = {"url": PINNED_STOCK}
    src = root / "production.json"
    src.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    # Reuse real importer fixture images; no video or audio in the archive.
    with zipfile.ZipFile(io.BytesIO(archive_bytes(doc))) as archive:
        for name in archive.namelist():
            if not name.startswith("assets/"):
                continue
            output = root / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(archive.read(name))
    return src


class MonoclipResourcePackTests(unittest.TestCase):
    def test_color_and_bw_archive_are_real_importer_compatible(self):
        for style in ("color_cinematic", "bw_cinematic"):
            with self.subTest(style=style), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                src = write_fixture(root, style)
                result = prepare_archive(src, root, root / "approved.zip")
                checked = inspect_archive((root / "approved.zip").read_bytes())
                self.assertEqual(checked["manifest"]["visual_style"], style)
                self.assertEqual(result["voice_requests_made"], 0)
                self.assertEqual(result["stock_downloads_made"], 0)
                self.assertFalse(result["ready_to_publish"])
                self.assertEqual(len([f for f in checked["files"] if f.startswith("assets/")]), 3)

    def test_existing_short_is_not_reclassified_as_monoclip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = write_fixture(root)
            doc = json.loads(src.read_text(encoding="utf-8"))
            del doc["visual_style"]
            doc["scenes"][1].pop("stock")
            src.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "opted-in"):
                prepare_archive(src, root, root / "not-monoclip.zip")

    def test_missing_image_and_duplicate_output_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            src = write_fixture(root)
            (root / "assets/scene_03.png").unlink()
            with self.assertRaises(FileNotFoundError):
                prepare_archive(src, root, root / "bad.zip")
            self.assertFalse((root / "bad.zip").exists())
            write_fixture(root)
            prepare_archive(src, root, root / "approved.zip")
            with self.assertRaises(FileExistsError):
                prepare_archive(src, root, root / "approved.zip")

    def test_symlink_cannot_escape_asset_root(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            root = Path(folder)
            src = write_fixture(root)
            original = root / "assets/scene_01.png"
            external = Path(outside) / "scene_01.png"
            original.replace(external)
            try:
                original.symlink_to(external)
            except (NotImplementedError, OSError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ValueError, "Unsafe asset path"):
                prepare_archive(src, root, root / "bad.zip")


class MonoclipVoiceRoutingTests(unittest.TestCase):
    def test_iapetus_selected_for_nidaa_and_ala_mahl_keeps_existing_path(self):
        plan = {"owner_authored": True, "short_template": "micro_story"}
        short = {"format": "short", "series_name": "نداء اليقظة"}
        self.assertEqual(
            _voice_performance_mode_for_brief(short, plan, ready_template="micro_story"),
            "nidaa_short_micro_story",
        )
        self.assertEqual(GEMINI38_NIDAA_SHORT_PRIMARY_VOICE, "Iapetus")
        self.assertEqual(GEMINI38_NIDAA_SHORT_PROVIDER, "gemini-3.8:Iapetus")
        ala_mahl = {"format": "short", "series_name": "على مهل"}
        self.assertEqual(
            _voice_performance_mode_for_brief(ala_mahl, plan, ready_template="ala_mahl"),
            "micro_story",
        )

    def test_lite_voice_cannot_silently_replace_iapetus(self):
        synth = GeminiOnlyVoiceSynthesizer(
            "local-dummy-never-sent", tts_model=GEMINI38_LITE_TTS_MODEL
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(
            RuntimeError, "Iapetus.*Flash TTS"
        ):
            synth.synthesize(
                "هذا اختبار صوت محلي لا ينبغي إرساله.",
                Path(folder) / "must-not-exist.wav",
                performance_mode="nidaa_short_micro_story",
            )


if __name__ == "__main__":
    unittest.main()
