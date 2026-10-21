"""Optional real-stock scenes in an approved ZIP (Nidaa Short, Film, and two ambient inserts in Ala Mahl), image fallback always."""
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
        self.assertEqual(package.STOCK_MAX_SCENES, {"short": 14, "film": 60, "podcast": 8})

    def test_podcast_takes_at_most_eight_stock_scenes(self):
        package.validate_manifest(with_stock(manifest("podcast"), [0]))
        self.assertEqual(package.STOCK_MAX_SCENES["podcast"], 8)

    def test_ala_mahl_takes_at_most_two_ambient_stock_scenes(self):
        base = manifest(template="ala_mahl")
        package.validate_manifest(with_stock(base, [0]))
        package.validate_manifest(with_stock(base, [0, 1]))
        self.assertEqual(package.ALA_MAHL_STOCK_MAX, 2)
        with self.assertRaises(Exception):
            package.validate_manifest(with_stock(base, [0, 1, 2]))

    def test_ala_mahl_stock_cannot_precede_a_continued_picture(self):
        doc = manifest(template="ala_mahl")
        doc["scenes"] = [s for s in doc["scenes"] if s["section_id"] != "s2"]  # s2 continues s1's picture
        with self.assertRaises(Exception):
            package.validate_manifest(with_stock(doc, [0]))
        package.validate_manifest(with_stock(doc, [1]))  # s3's own picture is not continued

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
    def test_ala_mahl_beat_carries_the_stock_request_and_its_approved_image(self):
        story = ready_production.build_story(with_stock(manifest(template="ala_mahl"), [1]))
        stock = [b for b in story["beats"] if b.get("owner_stock")]
        self.assertEqual(len(stock), 1)
        self.assertTrue(stock[0]["owner_asset"].startswith("assets/"))
        self.assertEqual(stock[0]["source_preference"], "stock_motion")

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

    def test_clip_matching_only_the_alternate_query_is_admitted(self):
        row = dict(self.GOOD, retrieval_description="close up of shoes on urban pavement")
        _clips, rights, _made = self.run_acquire(self.inner_returning(row))
        self.assertEqual(len([r for r in rights if r.get("provider") == "pexels"]), 1)

    def test_clip_matching_only_must_show_is_admitted(self):
        self.doc["scenes"][1]["stock"]["must_show"] = "hands opening workbook"
        atomic_write_json(self.root / "production.json", self.doc)
        self.story = ready_production.build_story(self.doc)
        row = dict(self.GOOD, retrieval_description="hands opening a workbook on a wooden table")
        _clips, rights, _made = self.run_acquire(self.inner_returning(row))
        self.assertEqual(len([r for r in rights if r.get("provider") == "pexels"]), 1)

    def test_unrelated_clip_is_still_rejected_even_with_three_descriptions(self):
        self.doc["scenes"][1]["stock"]["must_show"] = "hands opening workbook"
        atomic_write_json(self.root / "production.json", self.doc)
        self.story = ready_production.build_story(self.doc)
        self.assert_fell_back(self.inner_returning(dict(self.GOOD, retrieval_description="beautiful mountain landscape sunset")),
                              "stock_metadata")

    def test_report_names_the_reason_for_every_scene_that_asked_for_stock(self):
        from clean_v2.simple_mode import stock_outcomes

        self.assertEqual(stock_outcomes(self.root), {})
        atomic_write_json(self.root / "rights-manifest.json", {"assets": [
            {"beat_id": "scene_02", "stock_requested": True, "source_actual": "stock_motion", "owner_approved": False},
            {"beat_id": "scene_05", "stock_requested": True, "source_actual": "owner_art",
             "stock_fallback_reason": "stock_metadata_not_matching_query", "owner_approved": True},
            {"beat_id": "scene_07", "stock_requested": True, "source_actual": "owner_art",
             "replacement_reason": "visual_review_rejected", "stock_fallback_reason": "x", "owner_approved": True},
            {"beat_id": "scene_08", "source_actual": "owner_art"},
        ]})
        self.assertEqual(stock_outcomes(self.root), {"requested": 3, "used": 1, "fallbacks": [
            {"scene": "scene_05", "reason": "stock_metadata_not_matching_query"},
            {"scene": "scene_07", "reason": "visual_review_rejected"}]})

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


class ReplacementGeometryTests(unittest.TestCase):
    """The pipeline hands visual QA fmt="story" for a Short; a rejected stock clip replaced by the
    approved image must stay 9:16 (it was rendered 16:9 and centre-cropped ~3x in production)."""

    def test_rejected_stock_replacement_is_portrait_for_every_short_format_name(self):
        from clean_v2 import simple_mode

        for fmt in ("short", "story", "moment"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "scene.jpg").write_bytes(b"img")
                row = {"stock_requested": True, "approved_fallback_asset": "scene.jpg", "provider": "pexels"}
                with mock.patch.object(owner_art, "art_dir", return_value=root), \
                        mock.patch.object(owner_art, "make_motion_clip") as make:
                    self.assertTrue(simple_mode._use_approved_image(
                        root / "c.mp4", row, {"owner_motion": "hold"}, index=0, fmt=fmt, seconds=10.0, reason="x"))
                self.assertTrue(make.call_args.kwargs["portrait"])
                self.assertTrue(make.call_args.kwargs["respect_hold"])

    def test_film_replacement_stays_landscape(self):
        from clean_v2 import simple_mode

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scene.jpg").write_bytes(b"img")
            row = {"stock_requested": True, "approved_fallback_asset": "scene.jpg", "provider": "pexels"}
            with mock.patch.object(owner_art, "art_dir", return_value=root), \
                    mock.patch.object(owner_art, "make_motion_clip") as make:
                simple_mode._use_approved_image(root / "c.mp4", row, {}, index=0, fmt="film", seconds=10.0, reason="x")
            self.assertFalse(make.call_args.kwargs["portrait"])


if __name__ == "__main__":
    unittest.main()
