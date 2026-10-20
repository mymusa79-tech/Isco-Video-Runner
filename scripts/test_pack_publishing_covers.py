"""Editor-owned derived-Short cover and publishing, plus YouTube's own limits on every title/description/tag."""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import owner_art, production_package as package  # noqa: E402
from scripts import clean_v2_release_delivery as delivery  # noqa: E402
from scripts.test_production_package import archive_bytes, manifest  # noqa: E402


def png(size=(180, 320)):
    raw = io.BytesIO()
    Image.new("RGB", size, (30, 60, 90)).save(raw, "PNG")
    return raw.getvalue()


def podcast(**derivative):
    doc = manifest("podcast")
    doc["derivative"] = {"start_text": "لماذا تبدو الخطوة 1 صعبة أحيانًا؟", "end_text": "ثم نواصل.", **derivative}
    return doc


class ContractTests(unittest.TestCase):
    def test_derivative_cover_and_publishing_are_accepted(self):
        doc = podcast(cover={"asset": "assets/derived_cover.png", "text": "لماذا لا تبدأ؟"},
                      publishing={"title": "لماذا لا تبدأ رغم أنك تريد؟", "description": "مقتطف قصير.",
                                  "hashtags": ["#خارج_النص"], "tags": ["تسويف", "بداية"]})
        package.inspect_archive(archive_bytes(doc, extra={"assets/derived_cover.png": png()}))

    def test_derivative_cover_asset_must_be_in_the_archive(self):
        doc = podcast(cover={"asset": "assets/derived_cover.png", "text": "لماذا لا تبدأ؟"})
        with self.assertRaises(Exception):
            package.inspect_archive(archive_bytes(doc))

    def test_derivative_cover_text_is_short_and_fields_are_closed(self):
        for bad in ({"asset": "assets/d.png", "text": "a b c d e f g"}, {"asset": "assets/d.png"},
                    {"asset": "assets/d.png", "text": "x", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                package.validate_manifest(podcast(cover=bad))

    def test_old_derivative_without_extras_is_unchanged(self):
        doc = podcast()
        self.assertEqual(package.validate_manifest(copy.deepcopy(doc)), doc)

    def test_youtube_limits_are_enforced_for_main_and_derivative_publishing(self):
        def with_main(**changes):
            doc = manifest("film")
            doc["publishing"] = {**doc["publishing"], **changes}
            return doc

        package.validate_manifest(with_main(title="ع" * 100))
        package.validate_manifest(with_main(description="ع" * 2500))  # 5000 bytes exactly
        for bad in ({"title": "ع" * 101}, {"description": "ع" * 2501}, {"tags": "تسويف"}, {"tags": ["a,b"]},
                    {"tags": ["x"] * 16}, {"tags": ["ع" * 41]}, {"tags": ["ع" * 40] * 12}):
            with self.subTest(bad=list(bad)), self.assertRaises(Exception):
                package.validate_manifest(with_main(**bad))
        package.validate_manifest(with_main(tags=["تسويف", "بداية جديدة"]))
        with self.assertRaises(Exception):
            package.validate_manifest(podcast(publishing={"title": "ع" * 101, "description": "", "hashtags": []}))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_derived_short_uses_the_editors_title_description_and_tags(self):
        doc = podcast(publishing={"title": "لماذا تقرر كل ليلة ثم لا تبدأ؟", "description": "الوصف الكامل.",
                                  "hashtags": ["#خارج_النص", "#تسويف"], "tags": ["تسويف", "بداية"]})
        (self.root / "production.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        meta = delivery._derived_short_publish_metadata(self.root, parent_kind="podcast",
                                                        parent={"title": "الحلقة", "topic": "t"}, short_prefix="podcast-short")
        self.assertEqual((meta["title"], meta["description"], meta["hashtags"], meta["youtube_tags"]),
                         ("لماذا تقرر كل ليلة ثم لا تبدأ؟", "الوصف الكامل.", ["#خارج_النص", "#تسويف"], ["تسويف", "بداية"]))
        self.assertEqual((meta["kind"], meta["cover_file"], meta["video_file"]), ("derived_short", "derived-short-cover.jpg", "derived-short.mp4"))

    def test_without_owner_publishing_the_old_generated_metadata_is_kept(self):
        (self.root / "production.json").write_text(json.dumps(podcast(), ensure_ascii=False), encoding="utf-8")
        meta = delivery._derived_short_publish_metadata(self.root, parent_kind="podcast",
                                                        parent={"title": "الحلقة", "topic": "t"}, short_prefix="podcast-short")
        self.assertIn("خارج النص", meta["title"])
        self.assertTrue(meta["description"].startswith("مقتطف من:"))

    def test_main_publishing_carries_tags(self):
        doc = manifest("film")
        doc["publishing"]["tags"] = ["تسويف", "بداية"]
        (self.root / "production.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        meta = delivery._main_publish_metadata(self.root, kind="long", topic="t")
        self.assertEqual(meta["youtube_tags"], ["تسويف", "بداية"])
        self.assertNotIn("tags", meta)


class OwnerCoverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.art = Path(self._tmp.name) / "art"
        (self.art / "assets").mkdir(parents=True)
        self.out = Path(self._tmp.name) / "podcast-short-cover.jpg"
        self.out.write_bytes(b"generated")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, derivative):
        (self.art / "production.json").write_text(json.dumps({"derivative": derivative}), encoding="utf-8")

    def apply(self):
        with mock.patch.dict(os.environ, {owner_art.ART_DIR_ENV: str(self.art)}):
            return owner_art.apply_owner_derivative_cover(self.out, {"status": "pass"})

    def test_editor_cover_replaces_the_frame_crop_at_portrait_size(self):
        (self.art / "assets" / "d.png").write_bytes(png((1000, 1500)))
        self.write({"start_text": "a.", "end_text": "b.", "cover": {"asset": "assets/d.png", "text": "لماذا لا تبدأ؟"}})
        report = self.apply()
        self.assertEqual((report["source"], report["cover_text"], report["provider_calls_added"]),
                         ("owner_derivative_cover_image", "لماذا لا تبدأ؟", 0))
        with Image.open(self.out) as image:
            self.assertEqual(image.size, (1080, 1920))

    def test_without_a_derivative_cover_nothing_changes(self):
        self.write({"start_text": "a.", "end_text": "b."})
        self.assertEqual(self.apply(), {"status": "pass"})
        self.assertEqual(self.out.read_bytes(), b"generated")

    def test_a_broken_cover_is_fail_soft(self):
        self.write({"cover": {"asset": "assets/missing.png", "text": ""}})
        report = self.apply()
        self.assertTrue(report["owner_derivative_cover"].startswith("unavailable:"))
        self.assertEqual(self.out.read_bytes(), b"generated")


if __name__ == "__main__":
    unittest.main()
