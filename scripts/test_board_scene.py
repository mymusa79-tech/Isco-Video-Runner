"""Explainer board scenes in an approved Film / Podcast ZIP: contract, timing, rendering, fallbacks."""
from __future__ import annotations

import copy
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from clean_v2 import board_scene, owner_art, production_package as package, ready_production
from clean_v2.contracts import atomic_write_json
from scripts.test_production_package import archive_bytes, manifest

import os

os.environ.setdefault("BOARD_ALLOW_FALLBACK_FONT", "1")  # CI installs real Cairo; a bare sandbox may not have it

S2_ITEMS = ["في البداية بدت الخطوة", "لكنه أكملها بهدوء", "ورأى للمرة الأولى"]


def icon(color=(232, 89, 12), transparent=True) -> bytes:
    image = Image.new("RGBA", (300, 300), (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    ImageDraw.Draw(image).ellipse((40, 40, 260, 260), fill=color)
    raw = io.BytesIO()
    image.save(raw, "PNG")
    return raw.getvalue()


def board(layout="row", n=3, **extra):
    items = [{"label": f"الخطوة {i + 1}", "at_text": S2_ITEMS[i % 3], "asset": f"assets/board_{i + 1:02d}.png"} for i in range(n)]
    if n > 3:
        items = items[:3]
    return {"layout": layout, "title": "رحلة الخطوة", "items": items, **extra}


def film_doc(**board_kwargs):
    doc = manifest("film")
    doc["scenes"][1]["board"] = board(**board_kwargs)
    return doc


def pack(doc, extra_count=3):
    extra = {f"assets/board_{i + 1:02d}.png": icon() for i in range(extra_count)}
    return archive_bytes(doc, extra=extra)


class ContractTests(unittest.TestCase):
    def test_film_board_with_pictures_is_accepted(self):
        checked = package.inspect_archive(pack(film_doc()))
        self.assertIn("board", checked["manifest"]["scenes"][1])

    def test_board_picture_must_be_in_the_archive(self):
        with self.assertRaises(Exception):
            package.inspect_archive(archive_bytes(film_doc()))

    def test_items_may_have_no_picture(self):
        doc = film_doc()
        for item in doc["scenes"][1]["board"]["items"]:
            item.pop("asset")
        package.inspect_archive(archive_bytes(doc))

    def test_podcast_accepts_row_steps_grid_but_not_hub(self):
        for layout in ("row", "steps"):
            doc = manifest("podcast")
            doc["scenes"][1]["board"] = {"layout": layout, "items": [
                {"label": "الأول", "at_text": "لماذا تبدو الخطوة 2"}, {"label": "الثاني", "at_text": "في البداية بدت الخطوة"}]}
            package.validate_manifest(doc)
        doc["scenes"][1]["board"]["layout"] = "hub"
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_short_cannot_use_a_board(self):
        doc = manifest("short")
        doc["scenes"][1]["board"] = {"layout": "row", "items": [{"label": "أ", "at_text": "x"}] * 2}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_hook_scene_and_stock_scene_cannot_be_boards(self):
        doc = manifest("film")
        doc["scenes"][0]["board"] = {"layout": "row", "items": [
            {"label": "أ", "at_text": "لم يكن يحتاج"}, {"label": "ب", "at_text": "جلس أمام فنجانه"}]}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)
        doc = film_doc()
        doc["scenes"][1]["stock"] = {"query_en": "person walking on sidewalk"}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_at_text_must_be_literal_unique_and_in_order(self):
        for bad in (["غير موجودة تمامًا", S2_ITEMS[1], S2_ITEMS[2]], [S2_ITEMS[1], S2_ITEMS[0], S2_ITEMS[2]]):
            doc = film_doc()
            for item, text in zip(doc["scenes"][1]["board"]["items"], bad):
                item["at_text"] = text
            with self.subTest(bad=bad), self.assertRaises(Exception):
                package.validate_manifest(doc)

    def test_item_counts_and_label_length(self):
        doc = film_doc()
        doc["scenes"][1]["board"]["items"] = doc["scenes"][1]["board"]["items"][:1]
        with self.assertRaises(Exception):
            package.validate_manifest(doc)
        doc = film_doc()
        doc["scenes"][1]["board"]["items"][0]["label"] = "ك" * 31
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_too_many_boards_are_refused(self):
        doc = film_doc()
        doc["scenes"][2]["board"] = {"layout": "row", "items": [
            {"label": "أ", "at_text": "حين عاد إلى فنجانه"}, {"label": "ب", "at_text": "ابتسم للباب المفتوح"}]}
        package.validate_manifest(copy.deepcopy(doc))  # two boards are fine under the real cap
        with mock.patch.dict(board_scene.BOARD_MAX_SCENES, {"film": 1}), self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_derivative_may_not_cover_a_board(self):
        doc = film_doc()
        doc["derivative"] = {"start_text": "في البداية بدت الخطوة أصغر", "end_text": "تنتظره"}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)
        doc["derivative"] = {"start_text": "لم يكن يحتاج إلى يوم مثالي", "end_text": "فرصة حقيقية اليوم"}
        package.validate_manifest(doc)

    def test_packs_without_boards_are_unchanged(self):
        doc = manifest("film")
        self.assertEqual(package.validate_manifest(copy.deepcopy(doc)), doc)


class StoryTests(unittest.TestCase):
    def test_beat_carries_spec_and_its_own_text_span(self):
        story = ready_production.build_story(film_doc())
        beat = next(b for b in story["beats"] if b.get("owner_board"))
        self.assertEqual(beat["section_id"], "s2")
        self.assertTrue(beat["owner_board"]["text"].startswith("في البداية بدت الخطوة"))
        for text in S2_ITEMS:
            self.assertIn(text, beat["owner_board"]["text"])
        self.assertEqual(len([b for b in story["beats"] if b.get("owner_board")]), 1)


class TimingTests(unittest.TestCase):
    ITEMS = [{"at_text": "ألف"}, {"at_text": "باء"}, {"at_text": "جيم"}]
    TEXT = "ألف " + "كلمة " * 20 + "باء " + "كلمة " * 20 + "جيم " + "كلمة " * 20

    def test_reveals_follow_the_words_in_order_and_leave_the_tail_clear(self):
        times = board_scene.reveal_times(self.TEXT, self.ITEMS, 40.0)
        self.assertEqual(times, sorted(times))
        self.assertGreaterEqual(times[0], board_scene.REVEAL_FIRST)
        self.assertLessEqual(times[-1], 40.0 - board_scene.REVEAL_TAIL + 1e-6)
        self.assertGreater(times[1], 10.0)  # the second item waits for its words
        for left, right in zip(times, times[1:]):
            self.assertGreaterEqual(right - left, 0.69)

    def test_compressed_estimate_shows_everything_in_a_shorter_slot(self):
        times = board_scene.reveal_times(self.TEXT, self.ITEMS, 40.0, compress=0.7)
        self.assertLess(times[-1], 40.0 * 0.7 + 0.5)

    def test_very_short_slot_still_orders_every_item_inside_the_clip(self):
        times = board_scene.reveal_times(self.TEXT, self.ITEMS, 6.0)
        self.assertEqual(times, sorted(times))
        self.assertLess(times[-1], 6.0)

    def test_events_over_a_board_are_dropped(self):
        events = [{"start": 5.0, "end": 9.0}, {"start": 30.0, "end": 34.0}]
        kept = board_scene.drop_events_over_boards(events, [(4.0, 12.0)])
        self.assertEqual(kept, [events[1]])


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,nb_frames,r_frame_rate",
                          "-show_entries", "format=duration", "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class RenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "assets").mkdir()
        for i in range(3):
            (self.root / f"assets/board_{i + 1:02d}.png").write_bytes(icon())
        (self.root / "assets/photo.png").write_bytes(icon(transparent=False))

    def tearDown(self):
        self._tmp.cleanup()

    def render(self, spec, seconds, fmt="film"):
        times = board_scene.reveal_times(" ".join(["كلمة"] * 30), spec["items"], seconds)
        return board_scene.render_board_clip(spec, self.root, self.root / "out.mp4", seconds=seconds, times=times, fmt=fmt)

    def test_film_row_board_renders_full_hd_with_exact_length_and_fills_up(self):
        spec = board("row", 3)
        path = self.render(spec, 7.0)
        info = probe(path)
        self.assertEqual((info["streams"][0]["width"], info["streams"][0]["height"]), (1920, 1080))
        self.assertAlmostEqual(float(info["format"]["duration"]), 7.0, delta=0.1)
        first, last = self.root / "first.png", self.root / "last.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "0.05", "-i", str(path), "-frames:v", "1", str(first)], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.1", "-i", str(path), "-frames:v", "1", str(last)], check=True)
        a, b = Image.open(first).convert("L"), Image.open(last).convert("L")
        from PIL import ImageChops
        self.assertGreater(sum(ImageChops.difference(a, b).histogram()[40:]), 20000)  # the board filled up

    def test_podcast_steps_board_and_an_opaque_picture_render(self):
        spec = {"layout": "steps", "theme": "slate", "title": "ثلاث خطوات", "items": [
            {"label": "الأول", "at_text": "كلمة", "asset": "assets/photo.png"},
            {"label": "الثاني", "at_text": "كلمة"},
            {"label": "الثالث بتفصيل أطول قليلًا", "at_text": "كلمة", "asset": "assets/board_01.png"}]}
        info = probe(self.render(spec, 7.0, fmt="podcast"))
        self.assertAlmostEqual(float(info["format"]["duration"]), 7.0, delta=0.1)

    def test_bad_spec_raises_instead_of_producing_a_broken_clip(self):
        with self.assertRaises(board_scene.BoardError):
            board_scene.render_board_clip({"layout": "hub", "items": board("row", 3)["items"]}, self.root, self.root / "x.mp4",
                                          seconds=6, times=[1, 2, 3], fmt="podcast")


class FontTests(unittest.TestCase):
    def test_without_cairo_a_production_board_refuses_instead_of_using_another_font(self):
        board_scene._font_path.cache_clear()
        try:
            with mock.patch.dict(os.environ, {"BOARD_ALLOW_FALLBACK_FONT": "0"}), \
                    mock.patch.object(subprocess, "run", return_value=mock.Mock(stdout="DejaVu Sans|/x.ttf")):
                with self.assertRaises(board_scene.BoardError):
                    board_scene._font_path()
        finally:
            board_scene._font_path.cache_clear()


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.doc = film_doc()
        atomic_write_json(self.root / "production.json", self.doc)
        for name, raw in package.inspect_archive(pack(self.doc))["files"].items():
            if name != "production.json":
                (self.root / name).parent.mkdir(exist_ok=True)
                (self.root / name).write_bytes(raw)
        self.story = ready_production.build_story(self.doc)

    def tearDown(self):
        self._tmp.cleanup()

    def run_acquire(self, board_effect):
        made = []

        def fake_motion(_src, dst, **kw):
            Path(dst).write_bytes(b"img")
            made.append(Path(dst).name)
            return dst

        with mock.patch.object(owner_art, "make_motion_clip", side_effect=fake_motion), \
                mock.patch.object(board_scene, "render_board_clip", side_effect=board_effect):
            clips, rights = owner_art.OwnerArtVisualSource(mock.Mock(), self.root).acquire(
                {"visual_story": self.story}, self.root / "visuals", "film", 5, {f"s{i}": 60 for i in range(1, 6)})
        return clips, rights, made

    def test_board_clip_replaces_the_still_and_records_where_to_rebuild_it(self):
        def ok(spec, root, dest, **kw):
            Path(dest).write_bytes(b"board")
            return Path(dest)

        clips, rights, made = self.run_acquire(ok)
        row = next(r for r in rights if r.get("owner_board"))
        self.assertTrue(row["board_rendered"])
        self.assertEqual(row["owner_board_root"], str(self.root))
        self.assertEqual(row["provider"], "owner_art")  # owner art is never reviewed or replaced
        self.assertEqual(len(made), len(rights) - 1)

    def test_a_board_failure_falls_back_to_the_approved_image(self):
        def boom(*a, **k):
            raise RuntimeError("font missing")

        clips, rights, made = self.run_acquire(boom)
        row = next(r for r in rights if r.get("owner_board"))
        self.assertIn("RuntimeError", row["board_fallback_reason"])
        self.assertEqual(len(made), len(rights))
        self.assertEqual(len(clips), len(rights))


class ScheduledRerenderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.path = self.out / "visual-owner-image_2.mp4"
        self.path.write_bytes(b"estimate")
        self.rows = [{"local_file": self.path.name, "beat_id": "image_2", "owner_board_root": str(self.out),
                      "owner_board": {"spec": board("row", 3), "text": " ".join(S2_ITEMS) + " " + "كلمة " * 30}},
                     {"local_file": "visual-owner-image_1.mp4", "beat_id": "image_1"}]
        self.schedule = [{"path": self.path, "start": 10.0, "end": 31.0},
                         {"path": self.out / "visual-owner-image_1.mp4", "start": 0.0, "end": 10.0}]

    def tearDown(self):
        self._tmp.cleanup()

    def test_exact_slot_is_rendered_in_place_and_windows_are_recorded(self):
        calls = []

        def fake(spec, root, dest, *, seconds, times, fmt):
            calls.append((seconds, list(times), fmt))
            Path(dest).write_bytes(b"exact")
            return Path(dest)

        with mock.patch.object(board_scene, "render_board_clip", side_effect=fake):
            report = board_scene.rerender_scheduled(self.out, self.schedule, self.rows, "film")
        self.assertEqual(self.path.read_bytes(), b"exact")
        self.assertAlmostEqual(calls[0][0], 21.2)
        self.assertEqual(report[0]["status"], "exact")
        windows = board_scene.blocked_windows(self.out)
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0][0], 9.6)

    def test_failure_keeps_the_estimate_clip(self):
        with mock.patch.object(board_scene, "render_board_clip", side_effect=RuntimeError("x")):
            report = board_scene.rerender_scheduled(self.out, self.schedule, self.rows, "film")
        self.assertEqual(self.path.read_bytes(), b"estimate")
        self.assertEqual(report[0]["status"], "kept_estimate")

    def test_no_board_rows_means_no_report(self):
        self.assertEqual(board_scene.rerender_scheduled(self.out, self.schedule[1:], self.rows, "film"), [])
        self.assertEqual(board_scene.blocked_windows(self.out), [])


if __name__ == "__main__":
    unittest.main()
