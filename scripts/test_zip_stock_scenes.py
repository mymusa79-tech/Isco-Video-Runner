"""Optional real-stock scenes in an approved ZIP (Nidaa Short + Film only), image fallback always."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import owner_art, production_package as package, ready_production
from clean_v2.media import _editorial_boundary_decisions
from clean_v2.contracts import atomic_write_json
from scripts.test_production_package import archive_bytes, manifest

STOCK = {"query_en": "person walking on sidewalk", "query_alt_en": "walking shoes urban pavement"}


def with_stock(doc, indexes):
    doc = copy.deepcopy(doc)
    for i in indexes:
        doc["scenes"][i]["stock"] = dict(STOCK)
    return doc


class ContractTests(unittest.TestCase):
    def test_short_and_film_accept_a_few_stock_scenes(self):
        self.assertEqual(len([s for s in package.validate_manifest(with_stock(manifest("short"), [1]))["scenes"] if "stock" in s]), 1)
        package.inspect_archive(archive_bytes(with_stock(manifest("short"), [0, 1, 2])))
        package.inspect_archive(archive_bytes(with_stock(manifest("film"), [0, 1, 2, 3])))
        every = len(manifest("short")["scenes"])
        package.validate_manifest(with_stock(manifest("short"), list(range(every))))

    def test_stock_may_cover_every_scene(self):
        self.assertEqual(package.STOCK_MAX_SCENES, {"short": 7, "film": 30})

    def test_podcast_and_ala_mahl_never_take_stock(self):
        for doc in (with_stock(manifest("podcast"), [0]), with_stock(manifest(template="ala_mahl"), [0])):
            with self.assertRaises(Exception):
                package.validate_manifest(doc)

    def test_query_must_be_a_short_english_phrase_and_fields_are_closed(self):
        for bad in ({"query_en": "رجل يمشي"}, {"query_en": "walk"}, {"query_en": "a b c d e f g h i j"},
                    {"query_en": "person walking", "url": "https://x"}, {}):
            doc = manifest("short")
            doc["scenes"][0]["stock"] = bad
            with self.subTest(bad=bad), self.assertRaises(Exception):
                package.validate_manifest(doc)

    def test_old_packs_without_stock_are_unchanged(self):
        for fmt in ("short", "film", "podcast"):
            doc = manifest(fmt)
            self.assertEqual(package.validate_manifest(copy.deepcopy(doc)), doc)


class StoryTests(unittest.TestCase):
    def test_beat_keeps_the_approved_image_and_carries_the_stock_request(self):
        story = ready_production.build_story(with_stock(manifest("short"), [1]))
        beats = story["beats"]
        stock = [b for b in beats if b.get("owner_stock")]
        self.assertEqual(len(stock), 1)
        self.assertEqual(stock[0]["owner_asset"], "assets/scene_02.png")
        self.assertEqual(stock[0]["stock_query_en"], STOCK["query_en"])
        self.assertTrue(all(b["owner_asset"] for b in beats))


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.doc = with_stock(manifest("short"), [1])
        self.doc["scenes"][1]["transition"] = "cut"
        atomic_write_json(self.root / "production.json", self.doc)
        for name, raw in package.inspect_archive(archive_bytes(self.doc))["files"].items():
            if name != "production.json":
                (self.root / name).parent.mkdir(exist_ok=True)
                (self.root / name).write_bytes(raw)
        self.story = ready_production.build_story(self.doc)

    def tearDown(self):
        self._tmp.cleanup()

    def run_acquire(self, inner):
        made = []

        def fake_motion(_src, dst, **kw):
            Path(dst).write_bytes(b"img")
            made.append(Path(dst).name)
            return dst

        with mock.patch.object(owner_art, "make_motion_clip", side_effect=fake_motion):
            clips, rights = owner_art.OwnerArtVisualSource(inner, self.root).acquire(
                {"visual_story": self.story}, self.root / "visuals", "short", 5, {"s1": 40, "s2": 40, "s3": 40})
        return clips, rights, made

    def inner_returning(self, row):
        inner = mock.Mock()

        def acquire(plan, out, fmt, n, est):
            self.sub_plan = plan
            Path(out).mkdir(parents=True, exist_ok=True)
            clip = Path(out) / "visual-01.mp4"
            clip.write_bytes(b"clip")
            return [clip], [dict(row, local_file="visual-01.mp4")]

        inner.acquire.side_effect = acquire
        return inner

    GOOD = {"provider": "pexels", "asset_id": "1", "source_actual": "stock_motion",
            "retrieval_description": "person walking on sidewalk city pavement", "section_id": "s2"}

    def test_matching_real_stock_is_used_and_ai_route_is_closed(self):
        clips, rights, made = self.run_acquire(self.inner_returning(self.GOOD))
        stock_rows = [r for r in rights if r.get("provider") == "pexels"]
        self.assertEqual(len(stock_rows), 1)
        self.assertEqual(stock_rows[0]["approved_fallback_asset"], "assets/scene_02.png")
        self.assertEqual(stock_rows[0]["owner_transition"], "cut")
        self.assertTrue(stock_rows[0]["local_file"].startswith("visual-stock-"))
        self.assertEqual(len(clips), len(rights))
        self.assertEqual(len(made), len(rights) - 1)  # the stock beat made no image clip
        beat = self.sub_plan["visual_story"]["beats"][0]
        self.assertTrue(beat["_ai_fallback_pass"])
        self.assertNotIn("owner_asset", beat)

    def test_matching_stock_does_not_erase_approved_transition_when_rendered(self):
        for transition in ("cut", "dissolve"):
            with self.subTest(transition=transition):
                self.doc["scenes"][1]["transition"] = transition
                atomic_write_json(self.root / "production.json", self.doc)
                self.story = ready_production.build_story(self.doc)
                clips, rights, made = self.run_acquire(self.inner_returning(self.GOOD))
                self.assertEqual(len(clips), len(rights))
                stock_index = next(i for i, row in enumerate(rights) if row.get("provider") == "pexels")
                self.assertGreater(stock_index, 0)
                self.assertEqual(rights[stock_index]["owner_transition"], transition)
                signals = {row["local_file"]: row for row in rights}
                decisions = _editorial_boundary_decisions(
                    [Path(p) for p in clips],
                    [row["section_id"] for row in rights],
                    signals,
                )
                self.assertEqual(decisions[stock_index - 1]["decision"], transition.upper())
                self.assertEqual(decisions[stock_index - 1]["reason"], "approved_package_transition")

    def assert_fell_back(self, inner, reason_prefix):
        clips, rights, made = self.run_acquire(inner)
        self.assertEqual({r["asset_id"] for r in rights}, {s["asset"] for s in self.doc["scenes"]})
        row = next(r for r in rights if r.get("stock_requested"))
        self.assertTrue(row["stock_fallback_reason"].startswith(reason_prefix), row["stock_fallback_reason"])
        self.assertEqual(len(made), len(rights))

    def test_no_admitted_stock_falls_back_to_the_approved_image(self):
        self.assert_fell_back(self.inner_returning({"provider": "clean_v2_simple_fallback", "source_actual": "generated_background"}),
                              "no_admitted")

    def test_generic_or_unrelated_metadata_falls_back(self):
        row = dict(self.GOOD, retrieval_description="beautiful mountain landscape sunset")
        self.assert_fell_back(self.inner_returning(row), "stock_metadata")
        self.assert_fell_back(self.inner_returning(dict(self.GOOD, retrieval_description="")), "stock_metadata")

    def test_provider_error_never_blocks_production(self):
        inner = mock.Mock()
        inner.acquire.side_effect = RuntimeError("quota")
        self.assert_fell_back(inner, "stock_error:RuntimeError")

    def test_beats_without_stock_never_touch_the_stock_layer(self):
        self.story = ready_production.build_story(manifest("short"))
        inner = mock.Mock()
        self.run_acquire(inner)
        inner.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
