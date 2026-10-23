"""Editor-pinned stock (`stock.url`): exact Pexels/Pixabay video or photo, short clip continued by the approved image."""
from __future__ import annotations

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2 import owner_art, pinned_stock, production_package as package, ready_production, visual_qa  # noqa: E402
from clean_v2.contracts import atomic_write_json  # noqa: E402
from clean_v2.media import probe_duration  # noqa: E402
from clean_v2.owner_art import ART_DIR_ENV  # noqa: E402
from clean_v2.simple_mode import simple_visual_qa, stock_outcomes  # noqa: E402
from scripts.test_production_package import archive_bytes, manifest  # noqa: E402

PEXELS_VIDEO = "https://www.pexels.com/video/a-man-checking-his-phone-3195394/"
PEXELS_PHOTO = "https://www.pexels.com/ar-sa/photo/empty-bench-1234567/"
PIXABAY_VIDEO = "https://pixabay.com/videos/office-desk-work-99999/"
PIXABAY_PHOTO = "https://pixabay.com/photos/door-key-lock-4444444/"
COVERR_VIDEO = "https://coverr.co/videos/aerial-view-of-a-street-jixathxhhs"
FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def pinned(doc, indexes, urls):
    doc = copy.deepcopy(doc)
    for i, url in zip(indexes, urls):
        doc["scenes"][i]["stock"] = {"url": url}
    return doc


class ParseTests(unittest.TestCase):
    def test_valid_links(self):
        self.assertEqual(pinned_stock.parse_stock_url(PEXELS_VIDEO), ("pexels", "video", "3195394"))
        self.assertEqual(pinned_stock.parse_stock_url(PEXELS_PHOTO), ("pexels", "photo", "1234567"))
        self.assertEqual(pinned_stock.parse_stock_url("https://www.pexels.com/video/3195394/"), ("pexels", "video", "3195394"))
        self.assertEqual(pinned_stock.parse_stock_url(PIXABAY_VIDEO), ("pixabay", "video", "99999"))
        self.assertEqual(pinned_stock.parse_stock_url(PIXABAY_PHOTO), ("pixabay", "photo", "4444444"))
        self.assertEqual(pinned_stock.parse_stock_url(PIXABAY_VIDEO + "?utm=x"), ("pixabay", "video", "99999"))
        self.assertEqual(pinned_stock.parse_stock_url(COVERR_VIDEO + "?utm_source=chatgpt.com"), ("coverr", "video", "jixathxhhs"))
        self.assertEqual(pinned_stock.parse_stock_url("https://coverr.co/es/videos/street-view-AbC12345xy"), ("coverr", "video", "AbC12345xy"))

    def test_invalid_links(self):
        for bad in ("", None, 5, "http://www.pexels.com/video/a-3195394/", "https://evil.com/video/a-3195394/",
                    "https://www.pexels.com.evil.com/video/a-3195394/", "https://www.pexels.com/search/phone/",
                    "https://www.pexels.com/video/no-id/", "https://pixabay.com/music/song-12345/",
                    "https://pixabay.com/illustrations/art-12345/", "https://user:p@www.pexels.com/video/a-3195394/",
                    "https://www.pexels.com:8443/video/a-3195394/", "https://www.pexels.com/video/a-12/",
                    "https://coverr.co/videos/street", "https://coverr.co/s/street", "https://coverr.co.evil.com/videos/a-street-jixathxhhs",
                    "https://coverr.co/videos/a-street-short"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pinned_stock.parse_stock_url(bad)


class ContractTests(unittest.TestCase):
    @staticmethod
    def new_three_link_pack(fmt="short", template=None):
        doc = manifest(fmt, template=template)
        doc["stock_policy"] = package.THREE_LINK_STOCK_POLICY
        for i, scene in enumerate(doc["scenes"]):
            scene["stock"] = {
                "urls": [f"https://www.pexels.com/video/approved-scene-{7100000 + 3*i + j}/"
                         for j in range(3)],
                "must_show": "A visual matching this exact narration moment",
            }
        return doc

    def test_every_scene_has_three_prioritized_links_and_one_approved_picture(self):
        for fmt, template in (("short", None), ("film", None), ("podcast", None),
                              ("short", "ala_mahl")):
            doc = self.new_three_link_pack(fmt, template)
            with self.subTest(fmt=fmt, template=template):
                checked = package.validate_manifest(doc)
                # Validate a real packaged ZIP, not only the in-memory manifest.
                imported = package.inspect_archive(archive_bytes(doc))
                self.assertEqual(imported["manifest"]["stock_policy"], package.THREE_LINK_STOCK_POLICY)
                self.assertEqual(len(checked["scenes"]), len(doc["scenes"]))
                self.assertTrue(all(len(s["stock"]["urls"]) == 3 and s["asset"]
                                    for s in checked["scenes"]))
                beats = ready_production.build_story(checked)["beats"]
                for scene in checked["scenes"]:
                    beat = next(b for b in beats if b["id"] == scene["id"])
                    self.assertEqual(beat["owner_stock"]["urls"], scene["stock"]["urls"])
                    self.assertEqual(beat["owner_asset"], scene["asset"])

    def test_three_link_scene_stays_single_source_after_short_hook(self):
        doc = self.new_three_link_pack("short")
        beats = ready_production.build_story(doc)["beats"]
        self.assertEqual(len(beats), len(doc["scenes"]))
        self.assertFalse(any("__topic" in beat["id"] for beat in beats))
        self.assertTrue(all(beat.get("owner_stock", {}).get("urls") and beat["owner_asset"]
                            for beat in beats))
        # Older packs keep the existing hook/body split unchanged.
        old = ready_production.build_story(manifest("short"))["beats"]
        self.assertTrue(any("__topic" in beat["id"] for beat in old))

    def test_three_link_policy_refuses_missing_or_incomplete_scene_links(self):
        base = self.new_three_link_pack("film")
        variants = []
        missing = copy.deepcopy(base)
        missing["scenes"][2].pop("stock")
        variants.append(missing)
        partial = copy.deepcopy(base)
        partial["scenes"][0]["stock"]["urls"] = partial["scenes"][0]["stock"]["urls"][:2]
        variants.append(partial)
        single = copy.deepcopy(base)
        single["scenes"][0]["stock"] = {"url": PEXELS_VIDEO}
        variants.append(single)
        query = copy.deepcopy(base)
        query["scenes"][0]["stock"] = {"query_en": "walking along sidewalk"}
        variants.append(query)
        overlap = copy.deepcopy(base)
        overlap["scenes"][1]["stock"]["urls"][1] = overlap["scenes"][0]["stock"]["urls"][2]
        variants.append(overlap)
        for ix, doc in enumerate(variants):
            with self.subTest(case=ix), self.assertRaises(Exception):
                package.validate_manifest(doc)

    def test_three_link_policy_is_explicit_and_does_not_break_old_approved_zips(self):
        old = manifest("podcast")
        self.assertEqual(package.validate_manifest(copy.deepcopy(old)), old)
        new = self.new_three_link_pack("podcast")
        new["stock_policy"] = "nonsense"
        with self.assertRaises(Exception):
            package.validate_manifest(new)


    def test_url_replaces_the_query_and_may_carry_must_show(self):
        doc = package.validate_manifest(pinned(manifest("film"), [0, 1], [PEXELS_VIDEO, PIXABAY_PHOTO]))
        self.assertEqual(doc["scenes"][0]["stock"], {"url": PEXELS_VIDEO})
        doc = pinned(manifest("short"), [1], [PEXELS_PHOTO])
        doc["scenes"][1]["stock"]["must_show"] = "empty bench, no people"
        doc["scenes"][1]["stock"]["query_en"] = "empty waiting bench"
        package.inspect_archive(archive_bytes(doc))

    def test_bad_link_is_rejected(self):
        for bad in ("http://www.pexels.com/video/a-3195394/", "https://example.com/video/a-1234/", "pexels 3195394"):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                package.validate_manifest(pinned(manifest("film"), [0], [bad]))

    def test_same_clip_cannot_be_used_twice(self):
        with self.assertRaises(Exception):
            package.validate_manifest(pinned(manifest("film"), [0, 1], [PEXELS_VIDEO, PEXELS_VIDEO + "?x=1"]))
        package.validate_manifest(pinned(manifest("film"), [0, 1], [PEXELS_VIDEO, PIXABAY_VIDEO]))

    def test_pinned_scenes_count_toward_the_stock_cap(self):
        doc = pinned(manifest("podcast"), [0], [PEXELS_VIDEO])
        package.validate_manifest(doc)
        package.validate_manifest(pinned(manifest("short"), [0], [PEXELS_VIDEO]))

    def test_three_distinct_pinned_links_and_rejections(self):
        doc = manifest("short")
        scene = doc["scenes"][1]
        scene["stock"] = {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}
        package.validate_manifest(doc)
        for invalid in ([PEXELS_VIDEO, PEXELS_VIDEO + "?dup=1"],
                        [PEXELS_VIDEO, "https://bad.example/video/1"],
                        [PEXELS_VIDEO] * 4, []):
            scene["stock"] = {"urls": invalid}
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                package.validate_manifest(doc)
        scene["stock"] = {"url": PEXELS_VIDEO, "urls": [PIXABAY_VIDEO]}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_reuse_of_alternate_between_scenes_is_rejected(self):
        doc = manifest("film")
        doc["scenes"][0]["stock"] = {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO]}
        doc["scenes"][1]["stock"] = {"url": PIXABAY_VIDEO}
        with self.assertRaises(Exception):
            package.validate_manifest(doc)

    def test_scene_still_needs_its_approved_image(self):
        doc = pinned(manifest("film"), [0], [PEXELS_VIDEO])
        self.assertTrue(doc["scenes"][0]["asset"])
        del doc["scenes"][0]["asset"]
        with self.assertRaises(Exception):
            package.validate_manifest(doc)


class FetchTests(unittest.TestCase):
    def fetch(self, url, body, *, key="k", portrait=False):
        with mock.patch("clean_v2.media._read_secret", return_value=key), \
                mock.patch("clean_v2.media._get_json", return_value=body) as get:
            return pinned_stock.fetch_candidate(url, portrait=portrait), get

    def test_pexels_video_by_id(self):
        body = {"id": 3195394, "duration": 12, "url": PEXELS_VIDEO, "user": {"name": "Ann", "url": "https://x"},
                "video_files": [{"link": "https://videos.pexels.com/a.mp4", "width": 1280, "height": 720},
                                {"link": "https://videos.pexels.com/b.mp4", "width": 640, "height": 1136}]}
        cand, get = self.fetch(PEXELS_VIDEO, body)
        self.assertIn("/videos/videos/3195394", get.call_args.args[0])
        self.assertEqual((cand["provider"], cand["media_kind"], cand["duration"], cand["creator"]), ("pexels", "video", 12.0, "Ann"))
        self.assertTrue(cand["download_url"].endswith("a.mp4"))
        cand, _ = self.fetch(PEXELS_VIDEO, body, portrait=True)
        self.assertTrue(cand["download_url"].endswith("b.mp4"))

    def test_pexels_photo_by_id(self):
        cand, get = self.fetch(PEXELS_PHOTO, {"url": PEXELS_PHOTO, "photographer": "Bo", "src": {"large2x": "https://images.pexels.com/p.jpg"}})
        self.assertIn("/v1/photos/1234567", get.call_args.args[0])
        self.assertEqual((cand["media_kind"], cand["creator"], cand["download_url"]), ("photo", "Bo", "https://images.pexels.com/p.jpg"))

    def test_pixabay_video_and_photo_by_id(self):
        cand, get = self.fetch(PIXABAY_VIDEO, {"hits": [{"pageURL": PIXABAY_VIDEO, "user": "Cy", "duration": 9,
                                                       "videos": {"medium": {"url": "https://cdn.pixabay.com/v.mp4"}}}]})
        self.assertIn("/api/videos/", get.call_args.args[0])
        self.assertIn("id=99999", get.call_args.args[0])
        self.assertEqual((cand["media_kind"], cand["duration"], cand["download_url"]), ("video", 9.0, "https://cdn.pixabay.com/v.mp4"))
        cand, get = self.fetch(PIXABAY_PHOTO, {"hits": [{"pageURL": PIXABAY_PHOTO, "user": "Di", "largeImageURL": "https://cdn.pixabay.com/p.jpg"}]})
        self.assertIn("id=4444444", get.call_args.args[0])
        self.assertEqual((cand["media_kind"], cand["download_url"]), ("photo", "https://cdn.pixabay.com/p.jpg"))

    def test_coverr_video_by_id(self):
        cand, get = self.fetch(COVERR_VIDEO, {"id": "jixathxhhs", "duration": 14, "url": COVERR_VIDEO,
                                              "urls": {"mp4": "https://cdn.coverr.co/v.mp4"}})
        self.assertIn("api.coverr.co/videos/jixathxhhs", get.call_args.args[0])
        self.assertEqual((cand["provider"], cand["media_kind"], cand["duration"], cand["download_url"]),
                         ("coverr", "video", 14.0, "https://cdn.coverr.co/v.mp4"))

    def test_every_failure_has_a_short_reason(self):
        def reason(url, body=None, key="k", exc=None):
            with mock.patch("clean_v2.media._read_secret", return_value=key), \
                    mock.patch("clean_v2.media._get_json", return_value=body, side_effect=exc):
                with self.assertRaises(pinned_stock.PinnedStockError) as caught:
                    pinned_stock.fetch_candidate(url, portrait=False)
                return str(caught.exception)

        self.assertEqual(reason("https://example.com/video/a-1234/"), "pinned_url_invalid")
        self.assertEqual(reason(PEXELS_VIDEO, key=""), "pinned_no_api_key")
        self.assertEqual(reason(PIXABAY_VIDEO, key=""), "pinned_no_api_key")
        self.assertEqual(reason(COVERR_VIDEO, key=""), "pinned_no_api_key")
        self.assertEqual(reason(COVERR_VIDEO, {"urls": {}}), "pinned_no_playable_file")
        self.assertEqual(reason(COVERR_VIDEO, {"is_ai": True, "urls": {"mp4": "https://cdn.coverr.co/v.mp4"}}), "pinned_ai_generated")
        self.assertEqual(reason(PIXABAY_VIDEO, {"hits": []}), "pinned_asset_not_found")
        self.assertEqual(reason(PEXELS_VIDEO, {"video_files": []}), "pinned_no_playable_file")
        self.assertEqual(reason(PEXELS_VIDEO, exc=RuntimeError("HTTP 404")), "pinned_fetch_failed:RuntimeError")



class MultiLinkSelectionWithoutFFmpegTests(unittest.TestCase):
    """Never skip the three-link fallback contract when media binaries are unavailable."""

    def source(self):
        return owner_art.OwnerArtVisualSource(types.SimpleNamespace(), Path("."))

    def test_two_failed_links_then_third_succeeds(self):
        beat = {"id": "scene_01", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        expected = ((Path("clip.mp4"), {"provider": "coverr", "stock_pinned": True}), "")
        source = self.source()
        with mock.patch.object(source, "_try_pinned_one",
                               side_effect=[(None, "404"), (None, "no_playable_file"), expected]) as attempt:
            chosen, reason = source._try_pinned(beat, Path("approved.jpg"), Path("."), "short", 8.0, 0)
        self.assertEqual(reason, "")
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(chosen[1]["pinned_choice_rank"], 3)
        self.assertEqual(chosen[1]["pinned_alternates_total"], 3)
        self.assertEqual([v["reason"] for v in chosen[1]["pinned_prior_rejections"]],
                         ["404", "no_playable_file"])

    def test_all_three_failed_links_have_image_fallback_reason(self):
        beat = {"id": "scene_01", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        source = self.source()
        with mock.patch.object(source, "_try_pinned_one", return_value=(None, "blocked")) as attempt:
            chosen, reason = source._try_pinned(beat, Path("approved.jpg"), Path("."), "short", 8.0, 0)
        self.assertIsNone(chosen)
        self.assertEqual(attempt.call_count, 3)
        self.assertEqual(reason, "pinned_alternatives_exhausted:blocked,blocked,blocked")

    def test_three_urls_retry_undersized_video_without_using_fallback_photo(self):
        # This check runs even on CI hosts without ffmpeg.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = owner_art.OwnerArtVisualSource(types.SimpleNamespace(), root)
            beat = {"id": "scene_02",
                    "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]},
                    "owner_motion": "hold", "viewer_intent": "x"}
            cand = {"provider": "pexels", "asset_id": "3195394", "media_kind": "video",
                    "download_url": "https://videos.pexels.com/x", "source_url": PEXELS_VIDEO,
                    "creator": "", "creator_url": "", "duration": 3.0}
            def download(_url, destination):
                Path(destination).write_bytes(b"synthetic fake video")

            with mock.patch.object(pinned_stock, "fetch_candidate", return_value=cand), \
                 mock.patch("clean_v2.media._download_media", side_effect=download), \
                 mock.patch("clean_v2.media.probe_duration", return_value=3.0), \
                 mock.patch.object(owner_art, "make_motion_clip") as make_image:
                admitted, reason = source._try_pinned_one(
                    beat, root / "approved.jpg", root / "visuals",
                    "short", 10.0, 0, PEXELS_VIDEO,
                )
            self.assertIsNone(admitted)
            self.assertEqual(reason, "pinned_clip_too_short_for_scene")
            make_image.assert_not_called()
            self.assertFalse((root / "visuals/visual-stock-scene_02.mp4").exists())

    def test_three_short_videos_fall_back_to_sequence_not_still_art(self):
        beat = {"id": "scene_02", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        source = self.source()
        chained = ((Path("seq.mp4"), {"pinned_sequence": True}), "")
        with mock.patch.object(source, "_try_pinned_one",
                               return_value=(None, "pinned_clip_too_short_for_scene")), \
             mock.patch.object(source, "_try_pinned_sequence", return_value=chained) as seq:
            chosen, reason = source._try_pinned(beat, Path("a.jpg"), Path("."), "short", 8.0, 0,
                                                template="micro_story")
        self.assertEqual(reason, "")
        seq.assert_called_once()
        self.assertEqual(seq.call_args.args[0]["owner_stock"]["mode"], "sequence")
        self.assertTrue(chosen[1]["pinned_sequence_fallback_from_alternatives"])

    def test_sequence_fallback_never_for_blocked_podcast_or_ala_mahl(self):
        beat = {"id": "scene_02", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        source = self.source()
        cases = [("short", "micro_story", "blocked"), ("podcast", "", "pinned_clip_too_short_for_scene"),
                 ("short", "ala_mahl", "pinned_clip_too_short_for_scene")]
        for fmt, template, why in cases:
            with mock.patch.object(source, "_try_pinned_one", return_value=(None, why)), \
                 mock.patch.object(source, "_try_pinned_sequence") as seq:
                chosen, _ = source._try_pinned(beat, Path("a.jpg"), Path("."), fmt, 8.0, 0, template=template)
            self.assertIsNone(chosen)
            seq.assert_not_called()

    def test_legacy_single_url_keeps_its_failure_reason(self):
        beat = {"id": "scene_01", "owner_stock": {"url": PEXELS_VIDEO}}
        source = self.source()
        with mock.patch.object(source, "_try_pinned_one", return_value=(None, "pinned_no_api_key")) as attempt:
            chosen, reason = source._try_pinned(beat, Path("approved.jpg"), Path("."), "short", 8.0, 0)
        self.assertIsNone(chosen)
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(reason, "pinned_no_api_key")


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@unittest.skipUnless(FFMPEG, "ffmpeg required")
class PreparationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.asset = self.root / "approved.jpg"
        ffmpeg("-f", "lavfi", "-i", "color=c=orange:s=1280x720", "-frames:v", "1", str(self.asset))
        self.out = self.root / "visuals"
        self.out.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def source(self, *, preflight=None):
        inner = types.SimpleNamespace(media_preflight=preflight, media_transform=None)
        return owner_art.OwnerArtVisualSource(inner, self.root)

    def run_pinned(self, *, kind="video", length=3.0, seconds=10.0, fmt="film", preflight=None, error=None, w=1280, h=720):
        remote = self.root / ("remote.mp4" if kind == "video" else "remote.jpg")
        if kind == "video":
            ffmpeg("-f", "lavfi", "-i", f"testsrc=s={w}x{h}:d={length}:r=30", "-pix_fmt", "yuv420p", str(remote))
        else:
            ffmpeg("-f", "lavfi", "-i", f"testsrc=s={w}x{h}", "-frames:v", "1", str(remote))
        cand = {"provider": "pexels", "asset_id": "3195394", "media_kind": kind, "download_url": "https://videos.pexels.com/x",
                "source_url": PEXELS_VIDEO, "creator": "Ann", "creator_url": "", "duration": length}
        beat = {"id": "scene_02", "owner_stock": {"url": PEXELS_VIDEO}, "owner_motion": "slow_pan", "viewer_intent": "x"}

        def download(_url, destination):
            shutil.copy(remote, destination)

        fetch = mock.patch.object(pinned_stock, "fetch_candidate", return_value=cand, side_effect=error)
        with fetch, mock.patch("clean_v2.media._download_media", side_effect=download):
            return self.source(preflight=preflight)._try_pinned(beat, self.asset, self.out, fmt, seconds, 0)

    def test_three_url_fallback_selects_third_if_first_two_fail(self):
        beat = {"id": "scene_02", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        source = self.source()
        final = self.out / "stock.mp4"
        attempt = [(None, "pinned_asset_not_found"), (None, "pinned_clip_too_short"),
                   ((final, {"provider": "coverr", "stock_pinned": True}), "")]
        with mock.patch.object(source, "_try_pinned_one", side_effect=attempt) as worker:
            admitted, reason = source._try_pinned(beat, self.asset, self.out, "short", 5.0, 0)
        self.assertEqual(reason, "")
        self.assertEqual(worker.call_count, 3)
        self.assertEqual(admitted[1]["pinned_choice_rank"], 3)
        self.assertEqual(len(admitted[1]["pinned_prior_rejections"]), 2)

    def test_three_url_failure_returns_fallback_reason(self):
        beat = {"id": "scene_02", "owner_stock": {"urls": [PEXELS_VIDEO, PIXABAY_VIDEO, COVERR_VIDEO]}}
        source = self.source()
        with mock.patch.object(source, "_try_pinned_one", return_value=(None, "pinned_no_playable_file")) as worker:
            admitted, reason = source._try_pinned(beat, self.asset, self.out, "short", 5.0, 0)
        self.assertIsNone(admitted)
        self.assertEqual(worker.call_count, 3)
        self.assertTrue(reason.startswith("pinned_alternatives_exhausted:"))

    def test_short_clip_plays_then_the_approved_image_finishes_the_scene(self):
        admitted, reason = self.run_pinned(length=3.0, seconds=10.0)
        self.assertEqual(reason, "")
        clip, row = admitted
        self.assertGreaterEqual(probe_duration(clip), 10.0)
        self.assertLess(probe_duration(clip), 11.0)
        self.assertTrue(row["pinned_continues_with_image"])
        self.assertEqual(row["pinned_source_seconds"], 3.0)
        self.assertEqual((row["stock_pinned"], row["source_actual"], row["provider"], row["asset_id"]),
                         (True, "stock_motion", "pexels", "3195394"))
        self.assertEqual(clip.name, "visual-stock-scene_02.mp4")

    def test_clip_that_covers_the_scene_is_used_untouched(self):
        admitted, _ = self.run_pinned(length=12.0, seconds=8.0)
        clip, row = admitted
        self.assertNotIn("pinned_continues_with_image", row)
        self.assertAlmostEqual(probe_duration(clip), 12.0, delta=0.3)

    def test_vertical_short_continuation_is_portrait(self):
        admitted, _ = self.run_pinned(length=2.0, seconds=6.0, fmt="short", w=540, h=960)
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                              "-of", "csv=p=0", str(admitted[0])], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, "720,1280")
        self.assertGreaterEqual(probe_duration(admitted[0]), 6.0)
        self.assertLess(probe_duration(admitted[0]), 7.0)

    def test_photo_runs_like_an_owner_image(self):
        admitted, _ = self.run_pinned(kind="photo", seconds=7.0)
        clip, row = admitted
        self.assertEqual((row["source_actual"], row["media_kind"]), ("stock_still", "photo"))
        self.assertAlmostEqual(probe_duration(clip), 7.0, delta=0.3)

    def test_failures_return_a_reason_and_leave_no_file(self):
        self.assertEqual(self.run_pinned(length=1.0)[1], "pinned_clip_too_short")
        self.assertEqual(self.run_pinned(preflight=lambda p: {"local_media_rejection": "bad"})[1], "pinned_security_blocked")
        self.assertEqual(self.run_pinned(kind="photo", preflight=lambda p: {"x": 1})[1], "pinned_security_blocked")
        self.assertEqual(self.run_pinned(error=pinned_stock.PinnedStockError("pinned_asset_not_found"))[1], "pinned_asset_not_found")
        self.assertFalse((self.out / "visual-stock-scene_02.mp4").exists())


class AcquireTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.doc = pinned(manifest("short"), [1], [PEXELS_VIDEO])
        atomic_write_json(self.root / "production.json", self.doc)
        for name, raw in package.inspect_archive(archive_bytes(self.doc))["files"].items():
            if name != "production.json":
                (self.root / name).parent.mkdir(exist_ok=True)
                (self.root / name).write_bytes(raw)
        self.story = ready_production.build_story(self.doc)

    def tearDown(self):
        self._tmp.cleanup()

    def acquire(self, outcome):
        inner = mock.Mock()
        seen = {}

        def fake_motion(_src, dst, **kw):
            Path(dst).write_bytes(b"img")
            return dst

        def pinned_try(self_, beat, asset, out, fmt, seconds, index, template=""):
            seen["seconds"], seen["asset"] = seconds, asset.name
            return outcome

        with mock.patch.object(owner_art, "make_motion_clip", side_effect=fake_motion), \
                mock.patch.object(owner_art.OwnerArtVisualSource, "_try_pinned", pinned_try):
            clips, rights = owner_art.OwnerArtVisualSource(inner, self.root).acquire(
                {"visual_story": self.story}, self.root / "visuals", "short", 5, {"s1": 40, "s2": 40, "s3": 40})
        return clips, rights, inner, seen

    def test_a_pinned_scene_never_searches_and_is_marked(self):
        (self.root / "visuals").mkdir()
        clip = self.root / "visuals" / "visual-stock-scene_02.mp4"
        clip.write_bytes(b"c")
        row = {"provider": "pexels", "asset_id": "3195394", "local_file": clip.name, "source_actual": "stock_motion", "stock_pinned": True}
        clips, rights, inner, seen = self.acquire(((clip, row), ""))
        inner.acquire.assert_not_called()
        stock = next(r for r in rights if r.get("stock_pinned"))
        self.assertEqual((stock["stock_requested"], stock["stock_pinned_requested"], stock["owner_approved"]), (True, True, False))
        self.assertEqual(stock["approved_fallback_asset"], self.doc["scenes"][1]["asset"])
        self.assertEqual(seen["seconds"], 41)
        self.assertEqual(len(clips), len(rights))

    def test_a_failed_link_keeps_the_approved_image_with_the_reason(self):
        clips, rights, inner, _ = self.acquire((None, "pinned_asset_not_found"))
        inner.acquire.assert_not_called()
        self.assertEqual({r["asset_id"] for r in rights}, {s["asset"] for s in self.doc["scenes"]})
        row = next(r for r in rights if r.get("stock_requested"))
        self.assertEqual((row["stock_fallback_reason"], row["stock_pinned_requested"], row["owner_approved"]),
                         ("pinned_asset_not_found", True, True))

    def test_report_counts_pinned_scenes(self):
        atomic_write_json(self.root / "rights-manifest.json", {"assets": [
            {"beat_id": "a", "stock_requested": True, "stock_pinned_requested": True, "stock_pinned": True,
             "source_actual": "stock_motion", "owner_approved": False, "pinned_continues_with_image": True},
            {"beat_id": "b", "stock_requested": True, "stock_pinned_requested": True, "source_actual": "owner_art",
             "stock_fallback_reason": "pinned_asset_not_found", "owner_approved": True},
            {"beat_id": "c", "stock_requested": True, "source_actual": "stock_motion", "owner_approved": False},
        ]})
        self.assertEqual(stock_outcomes(self.root), {
            "requested": 3, "used": 2, "fallbacks": [{"scene": "b", "reason": "pinned_asset_not_found"}],
            "pinned_requested": 2, "pinned_used": 1, "pinned_continued_with_image": 1})


@unittest.skipUnless(FFMPEG, "ffmpeg required")
class ReviewTests(unittest.TestCase):
    """The client's picture review stays the last line of defence for a pinned clip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "visuals").mkdir()
        self.art = self.root / "art"
        (self.art / "assets").mkdir(parents=True)
        ffmpeg("-f", "lavfi", "-i", "color=c=orange:s=540x960:d=1", "-frames:v", "1", str(self.art / "assets" / "scene_04.jpg"))
        ffmpeg("-f", "lavfi", "-i", "color=c=red:s=270x480:d=2", "-pix_fmt", "yuv420p", str(self.root / "visuals" / "stock.mp4"))

    def tearDown(self):
        self._tmp.cleanup()

    def review(self, audit, **extra):
        row = {"provider": "pexels", "asset_id": "9", "local_file": "stock.mp4", "beat_id": "b4", "stock_pinned": True,
               "stock_requested": True, "approved_fallback_asset": "assets/scene_04.jpg", "source_actual": "stock_motion", **extra}
        env = {ART_DIR_ENV: str(self.art)}
        with mock.patch.dict(os.environ, env), mock.patch.object(visual_qa, "_apply_observed_visual_proof", lambda a: dict(audit)):
            simple_visual_qa(output_dir=self.root, plan={}, script={}, rights=[row], fmt="short")
        return row

    def test_unproven_match_does_not_reject_a_pinned_clip(self):
        row = self.review({"observed_proof_status": "not_matched"})
        self.assertEqual(row["provider"], "pexels")
        self.assertEqual(row["visual_verification"], "editor_pinned_unverified")

    def test_objective_risk_replaces_it_with_the_approved_image(self):
        for flag in ("identifiable_person", "prominent_logo_or_brand", "obvious_synthetic_or_visual_artifact"):
            with self.subTest(flag=flag):
                row = self.review({"observed_proof_status": "matched", flag: True})
                self.assertEqual(row["provider"], "owner_art")
                self.assertEqual(row["visual_verification"], "owner_designed_art")
                self.assertEqual(row["rejected_source"]["provider"], "pexels")

    def test_recognisable_face_replaces_it(self):
        row = self.review({"observed_proof_status": "matched", "observed_face_status": "recognizable"})
        self.assertEqual(row["provider"], "owner_art")

    def test_unpinned_stock_still_needs_a_proven_match(self):
        row = self.review({"observed_proof_status": "not_matched"}, stock_pinned=False)
        self.assertEqual(row["provider"], "owner_art")


if __name__ == "__main__":
    unittest.main()
