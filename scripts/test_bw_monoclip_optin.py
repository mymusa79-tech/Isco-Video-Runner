from __future__ import annotations

import copy
import unittest

from clean_v2.production_package import validate_manifest
from clean_v2.short_timed_text import build_rich_ass
from scripts.test_production_package import manifest


PEXELS_VIDEO = "https://www.pexels.com/video/a-man-walking-down-a-path-in-the-woods-28464399/"
OTHER_VIDEO = "https://www.pexels.com/video/person-walking-down-the-street-1234567/"
PHOTO = "https://www.pexels.com/photo/forest-trail-1234567/"


def bw_manifest():
    doc = manifest()
    doc["visual_style"] = "bw_cinematic"
    doc["scenes"][1]["stock"] = {"url": PEXELS_VIDEO}
    return doc


class BWCinematicSchemaTests(unittest.TestCase):
    def test_old_short_unchanged(self):
        doc = manifest()
        result = validate_manifest(doc)
        self.assertNotIn("visual_style", result)

    def test_accepts_one_or_two_video_alternatives(self):
        for links in ([PEXELS_VIDEO], [PEXELS_VIDEO, OTHER_VIDEO]):
            doc = bw_manifest()
            doc["scenes"][1]["stock"] = {"urls": links}
            self.assertEqual(validate_manifest(doc)["visual_style"], "bw_cinematic")

    def test_rejects_malformed_or_non_short_variants(self):
        cases = []
        doc = bw_manifest()
        doc["visual_style"] = "not_a_real_mode"
        cases.append(doc)
        doc = manifest(fmt="film")
        doc["visual_style"] = "bw_cinematic"
        cases.append(doc)
        doc = manifest(template="ala_mahl")
        doc["visual_style"] = "bw_cinematic"
        cases.append(doc)
        for doc in cases:
            with self.subTest(kind=doc["format"], template=doc["template"]):
                with self.assertRaises(Exception):
                    validate_manifest(doc)

    def test_rejects_sequence_policy_and_missing_main_video(self):
        alterations = (
            lambda d: d.update(stock_policy="three_urls_per_scene"),
            lambda d: d["scenes"][1]["stock"].update(mode="sequence"),
            lambda d: d["scenes"][1].pop("stock"),
            lambda d: d["scenes"][0].update(stock={"url": PEXELS_VIDEO}),
            lambda d: d["scenes"][1].update(stock={"url": PHOTO}),
            lambda d: d["editing"].update(music="youtube_audio_library"),
        )
        for transform in alterations:
            doc = bw_manifest()
            transform(doc)
            with self.subTest(doc=doc):
                with self.assertRaises(Exception):
                    validate_manifest(doc)


class BWCinematicCaptionTests(unittest.TestCase):
    def test_default_caption_render_is_unchanged(self):
        events = [
            {"start": 0.0, "end": 1.0, "text": "لن أتأخر اليوم", "role": "hook"},
            {"start": 1.0, "end": 2.0, "text": "أكمل الطريق", "role": "payoff"},
        ]
        regular = build_rich_ass(events)
        self.assertEqual(regular, build_rich_ass(events, bw_cinematic=False))
        mono = build_rich_ass(events, bw_cinematic=True)
        self.assertIn("&H00F5F5F5", mono)
        self.assertIn(r"\bord5\3c&H00000000&\shad0", mono)
        self.assertNotIn("&H00140B07&", mono)
        self.assertNotEqual(regular, mono)


if __name__ == "__main__":
    unittest.main()
