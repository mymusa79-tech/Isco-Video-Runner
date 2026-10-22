"""Priority 2/3 regression for approved program grammar and real delivery files."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from clean_v2.delivery_readiness import audit_delivery
from clean_v2.ready_production import build_story
from scripts.test_production_package import manifest


class ProgramIdentityContractTests(unittest.TestCase):
    def test_podcast_single_image_is_truly_static_even_for_legacy_zip_hints(self):
        doc = manifest("podcast")
        # An older approved archive is still accepted, but presentation is
        # fixed rather than endlessly zooming a single artwork for ten minutes.
        self.assertEqual(len({s["asset"] for s in doc["scenes"]}), 1)
        self.assertTrue(any(s["motion"] != "hold" for s in doc["scenes"]))
        original = copy.deepcopy(doc)
        beats = build_story(doc)["beats"]
        self.assertTrue(all(b["owner_motion"] == "hold" for b in beats))
        self.assertTrue(all(b["owner_transition"] == "cut" for b in beats))
        self.assertEqual(doc, original)

    def test_podcast_never_switches_to_board_even_with_legacy_metadata(self):
        doc = manifest("podcast")
        doc["scenes"][1]["board"] = {
            "layout": "compare", "title": "توضيح قديم",
            "items": [
                {"label": "الحالة الأولى", "at_text": "لماذا تبدو الخطوة 2"},
                {"label": "الحالة الثانية", "at_text": "في البداية بدت الخطوة"},
            ],
        }
        original = copy.deepcopy(doc)
        beats = build_story(doc)["beats"]
        self.assertTrue(all("owner_board" not in beat for beat in beats))
        self.assertTrue(all(beat["owner_motion"] == "hold" for beat in beats))
        self.assertEqual(doc, original)

    def test_multishot_podcast_preserves_approved_motion_transitions_and_stock(self):
        doc = manifest("podcast")
        doc["scenes"][1]["asset"] = "assets/scene_02.png"
        doc["scenes"][1]["stock"] = {"query_en": "person closing bedroom curtains"}
        beats = build_story(doc)["beats"]
        second = next(b for b in beats if b["id"] == doc["scenes"][1]["id"])
        self.assertEqual(second["owner_motion"], doc["scenes"][1]["motion"])
        self.assertEqual(second["owner_transition"], doc["scenes"][1]["transition"])
        self.assertEqual(second["owner_stock"]["query_en"], "person closing bedroom curtains")

    def test_multishot_podcast_uses_first_complete_a_turn_for_visual_hook(self):
        doc = manifest("podcast")
        doc["script"]["sections"][0]["turns"][0]["text"] = (
            "الساعة الواحدة وأربعون دقيقة. أغلقت الهاتف ثلاث مرات. لماذا لا أنام؟"
        )
        beats = build_story(doc)["beats"]
        self.assertEqual(beats[0]["role"], "hook")
        self.assertTrue(beats[0]["writer_anchor_ar"].startswith("الساعة الواحدة"))

    def test_films_and_regular_shorts_preserve_editor_approved_movements(self):
        for fmt in ("film", "short"):
            with self.subTest(fmt=fmt):
                doc = manifest(fmt)
                beats = build_story(doc)["beats"]
                for scene in doc["scenes"]:
                    matching = [b for b in beats if b["id"] == scene["id"]]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(matching[0]["owner_motion"], scene["motion"])
                    self.assertEqual(matching[0]["owner_transition"], scene["transition"])


class ExportedDeliveryEvidenceTests(unittest.TestCase):
    @staticmethod
    def _make_accepted_film(root):
        video = root / "final.mp4"
        video.write_bytes(b"test fixture representing the already inspected rendered master")
        (root / "final-master-qc.json").write_text('{"status":"pass"}', encoding="utf-8")
        Image.new("RGB", (1280, 720), (45, 54, 58)).save(root / "cover.jpg")
        (root / "film-captions.json").write_text('{"status":"pass"}', encoding="utf-8")
        (root / "film-captions.srt").write_text(
            "1\\n00:00:00,000 --> 00:00:02,000\\nكل كلمة محفوظة.\\n", encoding="utf-8"
        )
        return hashlib.sha256(video.read_bytes()).hexdigest()

    def test_exact_accepted_export_and_usable_cover_are_complete(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            snapshot = (root / "final.mp4").read_bytes()
            report = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["primary_video_accepted"])
            self.assertEqual(report["primary_cover"]["dimensions"], [1280, 720])
            self.assertEqual(report["derivative"]["status"], "not_requested")
            self.assertTrue((root / "delivery-readiness.json").is_file())
            self.assertEqual((root / "final.mp4").read_bytes(), snapshot)

    def test_mutated_final_is_reported_not_silently_passed(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            (root / "final.mp4").write_bytes(b"changed after QC")
            result = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(result["status"], "review_master")
            self.assertFalse(result["sha256_matches_inspected_export"])
            self.assertIn("final_export_sha256_changed_after_inspection", result["review_reasons"])

    def test_missing_approved_derivative_is_partial_not_parent_failure(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            (root / "production.json").write_text(
                json.dumps({"derivative": {"start_text": "أولًا.", "end_text": "ثم."}}),
                encoding="utf-8",
            )
            (root / "derivative-status.json").write_text('{"status":"failed"}', encoding="utf-8")
            result = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(result["status"], "partial_delivery")
            self.assertTrue(result["primary_video_accepted"])
            self.assertEqual(result["derivative"]["status"], "review")
            self.assertIn("approved_derivative_not_delivered", result["review_reasons"])

    def test_derivative_success_requires_reinspection_of_actual_video(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            (root / "production.json").write_text(
                json.dumps({"derivative": {"start_text": "أولًا.", "end_text": "ثم."}}),
                encoding="utf-8",
            )
            (root / "derivative-status.json").write_text('{"status":"pass"}', encoding="utf-8")
            (root / "long-short.mp4").write_bytes(b"invalid bytes not video")
            with mock.patch("clean_v2.media.inspect_final", side_effect=RuntimeError("invalid codec")):
                bad = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(bad["derivative"]["status"], "review")
            self.assertEqual(bad["derivative"]["actual_video_qc"]["status"], "unreadable")
            with mock.patch("clean_v2.media.inspect_final", return_value={
                "width": 1080, "height": 1920, "duration_seconds": 25.0, "sha256": "a" * 64
            }):
                better = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(better["derivative"]["status"], "pass")
            self.assertIn("derivative_cover_missing", better["review_reasons"])

    def test_invalid_or_tiny_cover_is_flagged_without_mutating_video(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            Image.new("RGB", (320, 180)).save(root / "cover.jpg")
            result = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertEqual(result["primary_cover"]["status"], "review")
            self.assertEqual(result["status"], "partial_delivery")
            self.assertTrue(result["primary_video_accepted"])

    def test_fallback_cover_does_not_masquerade_as_chatgpt_authored_cover(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            (root / "production.json").write_text(
                json.dumps({"cover": {"asset": None, "text": "عنوان"}}),
                encoding="utf-8",
            )
            report = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertTrue(report["primary_video_accepted"])
            self.assertEqual(report["primary_cover"]["status"], "pass")
            self.assertEqual(report["status"], "partial_delivery")
            self.assertIn("approved_cover_asset_not_in_zip", report["review_reasons"])

    def test_film_caption_success_needs_its_subtitle_sidecar(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            expected = self._make_accepted_film(root)
            (root / "film-captions.srt").unlink()
            result = audit_delivery(root, fmt="film", expected_sha256=expected)
            self.assertIn("spoken_srt_missing_after_success", result["review_reasons"])


if __name__ == "__main__":
    unittest.main()
