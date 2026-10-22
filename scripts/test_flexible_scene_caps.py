"""Scene and stock counts are planned per video; only technical safety limits remain."""
from __future__ import annotations

import copy
import unittest

from clean_v2 import production_package as package, ready_production
from scripts.test_production_package import archive_bytes, manifest


def film_with_scenes(count: int, fmt: str = "film"):
    """A package whose sections carry `count` literal sentences, one scene per sentence."""
    doc = manifest(fmt)
    sections = len(doc["script"]["sections"])
    per_section = [count // sections + (1 if i < count % sections else 0) for i in range(sections)]
    scenes, number = [], 0
    for index, section in enumerate(doc["script"]["sections"]):
        sentences = [f"الجملة رقم {index}{k} تحمل فكرة مستقلة وواضحة للمشاهد." for k in range(per_section[index])]
        section["text"] = " ".join(sentences)
        for sentence in sentences:
            number += 1
            scenes.append({"id": f"image_{number}", "section_id": section["id"],
                           "asset": f"assets/scene_{number:02d}.png", "start_text": sentence,
                           "motion": "slow_zoom" if number % 2 else "slow_pan", "transition": "dissolve"})
    doc["scenes"] = scenes
    return doc


class FlexibleCapTests(unittest.TestCase):
    def test_caps_are_safety_limits_not_a_fixed_count(self):
        self.assertEqual(package.SCENE_MAX, {"short": 14, "film": 60, "podcast": 40})
        self.assertEqual(package.STOCK_MAX_SCENES, {"short": 14, "film": 60, "podcast": 16})
        self.assertGreaterEqual(package.MAX_FILES, 60 + 4 + 1)

    def test_film_accepts_more_than_thirty_scenes_and_any_of_them_may_be_stock(self):
        for count in (8, 30, 31, 45, 60):
            doc = film_with_scenes(count)
            with self.subTest(count=count):
                package.validate_manifest(doc)
        doc = film_with_scenes(45)
        for scene in doc["scenes"][:38]:  # ~84% stock
            scene["stock"] = {"query_en": "person walking on sidewalk", "query_alt_en": "walking shoes urban pavement"}
        checked = package.validate_manifest(doc)
        self.assertEqual(sum(1 for s in checked["scenes"] if "stock" in s), 38)

    def test_forty_five_scene_film_archive_is_importable(self):
        checked = package.inspect_archive(archive_bytes(film_with_scenes(45)))
        self.assertEqual(len(checked["manifest"]["scenes"]), 45)

    def test_story_builder_keeps_every_planned_scene(self):
        doc = film_with_scenes(45)
        for scene in doc["scenes"][:38]:
            scene["stock"] = {"query_en": "person walking on sidewalk"}
        beats = ready_production.build_story(doc)["beats"]
        self.assertEqual(len(beats), 45)
        self.assertEqual(sum(1 for b in beats if b.get("owner_stock")), 38)

    def test_limits_still_reject_runaway_packages(self):
        with self.assertRaises(Exception):
            package.validate_manifest(film_with_scenes(61))
        package.validate_manifest(film_with_scenes(14, "short"))
        with self.assertRaises(Exception):
            package.validate_manifest(film_with_scenes(15, "short"))


if __name__ == "__main__":
    unittest.main()
