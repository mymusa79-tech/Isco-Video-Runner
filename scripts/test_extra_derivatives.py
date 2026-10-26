from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2 import derived_short, owner_art, production_package as package
from clean_v2.contracts import atomic_write_json
from scripts import clean_v2_release_delivery as delivery
from scripts.test_production_package import manifest


def _pair(doc: dict, index: int) -> dict:
    """The complete A question and the last sentence of the complete B turn of one section."""
    question, answer = [t["text"] for t in doc["script"]["sections"][index]["turns"]]
    return {"start_text": question, "end_text": answer}


def episode(**changes) -> dict:
    doc = manifest("podcast")
    doc["derivative"] = {**_pair(doc, 0), "publishing": {"title": "الشورت الأول", "description": "د", "hashtags": ["#Shorts"]}}
    doc["extra_derivatives"] = [{**_pair(doc, 1), "publishing": {"title": "الشورت الثاني", "description": "د", "hashtags": ["#Shorts"]}}]
    doc.update(changes)
    return doc


class ContractTests(unittest.TestCase):
    def test_primary_plus_extras_are_accepted_and_legacy_single_derivative_is_unchanged(self):
        self.assertEqual(len(package.validate_manifest(episode())["extra_derivatives"]), 1)
        legacy = episode()
        del legacy["extra_derivatives"]
        self.assertNotIn("extra_derivatives", package.validate_manifest(legacy))

    def test_extras_need_the_primary_derivative_and_stay_within_four(self):
        doc = episode()
        del doc["derivative"]
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(doc)
        doc = episode()
        doc["extra_derivatives"] = [copy.deepcopy(doc["extra_derivatives"][0]) for _ in range(package.MAX_EXTRA_DERIVATIVES + 1)]
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(doc)
        doc["extra_derivatives"] = []
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(doc)

    def test_each_short_is_its_own_excerpt_cover_and_title(self):
        overlapping = episode()
        overlapping["extra_derivatives"][0].update(_pair(overlapping, 0))
        overlapping["extra_derivatives"][0]["publishing"]["title"] = "عنوان مختلف"
        with self.assertRaises(package.OwnerPackError) as raised:
            package.validate_manifest(overlapping)
        self.assertIn("تتداخل", str(raised.exception))
        same_title = episode()
        same_title["extra_derivatives"][0]["publishing"]["title"] = same_title["derivative"]["publishing"]["title"]
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(same_title)
        same_cover = episode()
        for item in (same_cover["derivative"], same_cover["extra_derivatives"][0]):
            item["cover"] = {"asset": "assets/shared.png", "text": "سؤال"}
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(same_cover)
        main_cover = episode()
        main_cover["cover"] = {"asset": "assets/main.png", "text": "خطوة"}
        main_cover["extra_derivatives"][0]["cover"] = {"asset": "assets/main.png", "text": "سؤال"}
        with self.assertRaises(package.OwnerPackError):
            package.validate_manifest(main_cover)

    def test_extra_short_publishing_cannot_name_the_other_channel(self):
        from scripts.test_ala_mahl_podcast import _ala_mahl_doc
        doc = _ala_mahl_doc()
        doc["derivative"] = {**_pair(doc, 0), "publishing": {"title": "الشورت الأول", "description": "د", "hashtags": ["#على_مهل"]}}
        doc["extra_derivatives"] = [{**_pair(doc, 1), "publishing": {"title": "الثاني", "description": "د", "hashtags": ["#Shorts"]}}]
        self.assertEqual(package.validate_manifest(copy.deepcopy(doc))["template"], "ala_mahl_dialogue")
        doc["extra_derivatives"][0]["publishing"]["description"] = "من خارج النص"
        with self.assertRaises(package.OwnerPackError) as raised:
            package.validate_manifest(doc)
        self.assertIn("extra_derivatives[1].publishing", str(raised.exception))

    def test_every_candidate_is_listed_primary_first(self):
        doc = episode()
        listed = derived_short.derivative_candidates(doc)
        self.assertEqual([c["start_text"] for c in listed],
                         [doc["derivative"]["start_text"], doc["extra_derivatives"][0]["start_text"]])
        self.assertEqual(derived_short.derivative_candidates({}), [])


def parent_two_excerpts(root: Path, doc: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    pairs = [[t["text"] for t in s["turns"]] for s in doc["script"]["sections"]]
    rows = [{"section_id": f"s{n}", "chunk": c, "role": "topic", "start": a, "end": b,
             "duration_seconds": b - a, "file": f"audio/{n}-{c}.wav"}
            for n, c, a, b in ((1, 1, 2.0, 6.0), (1, 2, 6.0, 14.0), (2, 1, 16.0, 20.0), (2, 2, 20.0, 30.0))]
    atomic_write_json(root / "production.json", doc)
    atomic_write_json(root / "timeline-first.json", {"status": "pass", "format": "podcast", "audio_units": rows})
    atomic_write_json(root / "voice-sections.json", {"sections": [
        {"id": f"s{n}", "chunks": [{"chunk": 1, "role": "topic", "text": "A: " + q},
                                   {"chunk": 2, "role": "topic", "text": "B: " + a}]}
        for n, (q, a) in enumerate(pairs, 1)]})
    atomic_write_json(root / "script.json", {"sections": [
        {"id": f"s{n}", "narration": f"A: {q} B: {a}"} for n, (q, a) in enumerate(pairs, 1)]})
    atomic_write_json(root / "run-manifest.json", {"status": "pass", "format": "podcast", "topic": doc["title"]})
    atomic_write_json(root / "final-master-qc.json", {"status": "pass"})
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x0B1220:s=480x270:r=30:d=32", "-f", "lavfi", "-i",
                    "sine=frequency=330:sample_rate=24000:duration=32", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(root / "final.mp4")], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(root / "final.mp4"),
                    "-vn", "-c:a", "pcm_s16le", str(root / "narration-mastered.wav")], check=True)


class ExtractionTests(unittest.TestCase):
    def test_primary_and_extra_shorts_are_cut_offline_from_the_same_frozen_master(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_two_excerpts(root, episode())
            before = hashlib.sha256((root / "final.mp4").read_bytes()).hexdigest()
            primary = derived_short.attempt(root)
            self.assertEqual(primary["status"], "pass", primary)
            extras = derived_short.attempt_extras(root)
            self.assertEqual([r["status"] for r in extras], ["pass"], extras)
            self.assertEqual(extras[0]["index"], 2)
            self.assertEqual(extras[0]["file"], "podcast-short-2.mp4")
            self.assertAlmostEqual(primary["duration_seconds"], 12 + derived_short.OUTRO_QUIET_SECONDS + derived_short.OUTRO_SECONDS, delta=0.3)
            self.assertAlmostEqual(extras[0]["duration_seconds"], 14 + derived_short.OUTRO_QUIET_SECONDS + derived_short.OUTRO_SECONDS, delta=0.3)
            self.assertEqual((extras[0]["tts_calls_added"], extras[0]["provider_calls_added"]), (0, 0))
            for name in ("podcast-short-2.mp4", "podcast-short-2-qc.json", "podcast-short-2.json", "derivative-extras.json"):
                self.assertTrue((root / name).is_file(), name)
            self.assertEqual(hashlib.sha256((root / "final.mp4").read_bytes()).hexdigest(), before)
            self.assertEqual(json.loads((root / "derivative-status.json").read_text())["file"], "podcast-short.mp4")

    def test_one_failed_or_overlapping_extra_never_changes_the_primary_or_the_others(self):
        doc = episode()
        doc["extra_derivatives"] = [{**_pair(doc, 0)}, {**_pair(doc, 1)}]  # first repeats the primary window
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_two_excerpts(root, doc)
            primary = derived_short.attempt(root)
            self.assertEqual(primary["status"], "pass", primary)
            extras = derived_short.attempt_extras(root)
            self.assertEqual([r["status"] for r in extras], ["skipped", "pass"], extras)
            self.assertIn("overlaps", extras[0]["reason"])
            self.assertFalse((root / "podcast-short-2.mp4").exists())
            self.assertTrue((root / "podcast-short-3.mp4").is_file())
            self.assertTrue((root / "podcast-short.mp4").is_file())

    def test_encoder_failure_is_a_report_not_an_exception(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_two_excerpts(root, episode())
            derived_short.attempt(root)
            extras = derived_short.attempt_extras(root, renderer=mock.Mock(side_effect=RuntimeError("encoder")))
            self.assertEqual(extras[0]["status"], "failed")
            self.assertFalse((root / "podcast-short-2.mp4").exists())

    def test_no_extras_means_no_work_and_no_files(self):
        doc = episode()
        del doc["extra_derivatives"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_two_excerpts(root, doc)
            derived_short.attempt(root)
            self.assertEqual(derived_short.attempt_extras(root), [])
            self.assertFalse((root / "derivative-extras.json").exists())


class CoverAndPublishingTests(unittest.TestCase):
    def test_each_extra_short_takes_its_own_owner_cover(self):
        with tempfile.TemporaryDirectory() as temporary:
            art = Path(temporary) / "art"
            (art / "assets").mkdir(parents=True)
            doc = episode()
            doc["derivative"]["cover"] = {"asset": "assets/c1.png", "text": "الأول"}
            doc["extra_derivatives"][0]["cover"] = {"asset": "assets/c2.png", "text": "الثاني"}
            (art / "production.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            Image.new("RGB", (540, 960), (200, 0, 0)).save(art / "assets/c1.png")
            Image.new("RGB", (540, 960), (0, 0, 200)).save(art / "assets/c2.png")
            with mock.patch.dict(os.environ, {owner_art.ART_DIR_ENV: str(art)}):
                first, second = Path(temporary) / "a.jpg", Path(temporary) / "b.jpg"
                self.assertEqual(owner_art.apply_owner_derivative_cover(first, {})["cover_text"], "الأول")
                report = owner_art.apply_owner_derivative_cover(second, {}, index=1)
                self.assertEqual(report["cover_text"], "الثاني")
                self.assertEqual(report["source"], "owner_derivative_cover_image")
                missing = owner_art.apply_owner_derivative_cover(Path(temporary) / "c.jpg", {"keep": 1}, index=2)
                self.assertEqual(missing, {"keep": 1})  # no cover planned: Cover Lite stays
            with Image.open(first) as one, Image.open(second) as two:
                self.assertGreater(one.getpixel((50, 50))[0], 150)
                self.assertGreater(two.getpixel((50, 50))[2], 150)

    def test_extra_short_publishes_with_its_own_title_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_write_json(root / "production.json", episode())
            atomic_write_json(root / "podcast-short-2.json", {"section_id": "s2"})
            meta = delivery._derived_short_publish_metadata(
                root, parent_kind="podcast", parent={"topic": "حلقة", "title": "حلقة"},
                short_prefix="podcast-short-2", extra_index=1)
            self.assertEqual(meta["title"], "الشورت الثاني")
            self.assertEqual(meta["video_file"], "derived-short-2.mp4")
            self.assertEqual(meta["cover_file"], "derived-short-2-cover.jpg")
            first = delivery._derived_short_publish_metadata(
                root, parent_kind="podcast", parent={"topic": "حلقة", "title": "حلقة"}, short_prefix="podcast-short")
            self.assertEqual((first["title"], first["video_file"]), ("الشورت الأول", "derived-short.mp4"))


class HookTests(unittest.TestCase):
    def test_hook_is_optional_short_and_applies_to_primary_and_extras(self):
        doc = episode()
        doc["derivative"]["hook"] = "لماذا نسهر رغم التعب؟"
        doc["extra_derivatives"][0]["hook"] = "الاعتراض الذي يقلب الفكرة"
        self.assertEqual(package.validate_manifest(doc)["derivative"]["hook"], "لماذا نسهر رغم التعب؟")
        self.assertNotIn("hook", package.validate_manifest(episode())["derivative"])

    def test_hook_over_40_characters_or_7_words_is_refused(self):
        for bad in ("ك" * 41, "كلمة " * 8, "", 5):
            doc = episode()
            doc["derivative"]["hook"] = bad
            with self.assertRaises(package.OwnerPackError, msg=repr(bad)):
                package.validate_manifest(doc)

    def test_hook_card_ends_before_the_closing_card_and_is_visual_only(self):
        ass = derived_short._hook_ass("لماذا نسهر؟", 12.0)
        self.assertIn("0:00:00.15,0:00:02.60", ass)
        self.assertIn("لماذا نسهر؟", ass)
        short = derived_short._hook_ass("لماذا نسهر؟", 7.2)
        self.assertIn("0:00:00.15,0:00:02.10", short)  # 7.2 - 5.0 - 0.1

    def test_outro_follows_the_channel_template_never_the_other_channels_logo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for template, expected in (("dialogue_qa", "short_outro"), ("ala_mahl_dialogue", "ala_mahl_outro"),
                                       ("ala_mahl", "ala_mahl_outro"), ("film_whatever", "short_outro")):
                atomic_write_json(root / "production.json", {"template": template})
                self.assertEqual(derived_short._outro_for(root).stem, expected, template)
            (root / "production.json").unlink()
            self.assertEqual(derived_short._outro_for(root).stem, "short_outro")

    def test_ala_mahl_derived_short_ends_with_its_own_outro(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = episode()
            doc["template"] = "ala_mahl_dialogue"
            parent_two_excerpts(root, doc)
            report = derived_short.attempt(root)
            self.assertEqual(report["status"], "pass", report)
            self.assertEqual(report["outro"], "ala_mahl_outro")
            out = root / "podcast-short.mp4"
            png = root / "end.png"
            subprocess.check_call(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{report['duration_seconds'] - 0.3:.2f}", "-i", str(out), "-frames:v", "1", str(png)])
            ref = root / "ref.png"
            subprocess.check_call(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.9", "-i", str(derived_short._outro_for(root)), "-frames:v", "1", str(ref)])
            nidaa = root / "nidaa.png"
            subprocess.check_call(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.9", "-i", str(derived_short.identity_sequence_outro_nidaa()), "-frames:v", "1", str(nidaa)])
            def sig(path):
                return list(Image.open(path).convert("L").resize((27, 48)).getdata())
            def dist(a, b):
                return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
            self.assertLess(dist(sig(png), sig(ref)), dist(sig(png), sig(nidaa)))

    def test_every_derived_short_ends_with_the_nidaa_outro_after_the_last_word(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent_two_excerpts(root, episode())
            report = derived_short.attempt(root)
            self.assertEqual(report["status"], "pass", report)
            self.assertEqual(report["outro"], "short_outro")
            self.assertAlmostEqual(report["clip_seconds"], 12, delta=0.1)
            out = root / "podcast-short.mp4"
            probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(out)]))
            kinds = {s["codec_type"] for s in probe["streams"]}
            self.assertEqual(kinds, {"video", "audio"})
            # The last 2.2 s are the owner's outro, not the clip: its frame differs from the clip's last frame.
            frames = []
            for at in (report["clip_seconds"] - 0.5, report["duration_seconds"] - 1.0):
                png = root / f"f{int(at*10)}.png"
                subprocess.check_call(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{at:.2f}", "-i", str(out), "-frames:v", "1", str(png)])
                frames.append(Image.open(png).convert("L").resize((54, 96)).tobytes())
            self.assertNotEqual(frames[0], frames[1])

    def test_hook_is_rendered_and_reported_without_changing_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            doc = episode()
            doc["derivative"]["hook"] = "لماذا نسهر رغم التعب؟"
            parent_two_excerpts(root, doc)
            primary = derived_short.attempt(root)
            self.assertEqual(primary["status"], "pass", primary)
            self.assertEqual(primary["hook_text"], "لماذا نسهر رغم التعب؟")
            self.assertAlmostEqual(primary["clip_seconds"], 12, delta=0.1)
            extras = derived_short.attempt_extras(root)
            self.assertEqual(extras[0]["status"], "pass", extras)
            self.assertIsNone(extras[0]["hook_text"])



if __name__ == "__main__":
    unittest.main()
