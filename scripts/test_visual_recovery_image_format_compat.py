from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from clean_v2 import media, visual_qa


class Run187VisualRecoveryFormatTests(unittest.TestCase):
    def test_still_renderer_handles_qa_story_and_all_production_formats(self):
        for fmt, dimensions in (
            ("story", "1080:1920"),  # Run187: QA's name for Short
            ("short", "1080:1920"),
            ("film", "1920:1080"),
            ("podcast", "1920:1080"),
        ):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                src, dst = root / "source.jpg", root / "candidate.mp4"
                src.write_bytes(b"image")
                calls = []

                def fake_ffmpeg(argv, *, timeout):
                    calls.append((argv, timeout))
                    Path(argv[-1]).write_bytes(b"video" * 300)

                with mock.patch.object(media, "_run", side_effect=fake_ffmpeg):
                    self.assertEqual(media._render_ai_still(src, dst, fmt=fmt), dst)
                self.assertEqual(len(calls), 1)
                args, timeout = calls[0]
                self.assertEqual(timeout, 240)
                self.assertIn("s=" + dimensions, args[args.index("-vf") + 1])
                self.assertGreaterEqual(dst.stat().st_size, 1024)

    def test_unrecognized_render_format_remains_blocked_before_ffmpeg(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(media, "_run") as run:
            with self.assertRaisesRegex(RuntimeError, "ai_still_render_format_unsupported"):
                media._render_ai_still(Path(tmp) / "photo.jpg", Path(tmp) / "x.mp4", fmt="unknown")
            run.assert_not_called()

    def test_actual_qa_story_photo_recovery_admits_two_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            accepted = []
            source = media.StockVisualSource(
                media_preflight=lambda path: accepted.append(("security", path.name)) or None,
                media_transform=lambda path: accepted.append(("transform", path.name)) or path,
            )
            pexels = {"provider": "pexels", "asset_id": "p187", "download_url": "https://example.invalid/p.jpg",
                      "metadata_semantic_score": 0.9, "local_rank_score": 0.7}
            pixabay = {"provider": "pixabay", "asset_id": "x187", "download_url": "https://example.invalid/x.jpg",
                       "metadata_semantic_score": 0.8, "local_rank_score": 0.8}

            def fake_ffmpeg(argv, *, timeout):
                Path(argv[-1]).write_bytes(b"video" * 300)

            with mock.patch.object(source, "_pexels_photo", return_value=pexels), mock.patch.object(
                source, "_pixabay_photo", return_value=pixabay
            ), mock.patch.object(source, "_pexels_recovery_pool", side_effect=AssertionError("extra video search")), mock.patch.object(
                source, "_pixabay_recovery_pool", side_effect=AssertionError("extra video search")
            ), mock.patch.object(source, "_coverr_recovery_pool", side_effect=AssertionError("extra video search")), mock.patch.object(
                media, "_download_media", side_effect=lambda url, dest: Path(dest).write_bytes(b"photo")
            ), mock.patch.object(media, "_run", side_effect=fake_ffmpeg):
                matches = source.acquire_replacement_candidates(
                    "back view of person slumped over in a chair", root,
                    "story", destination_name="visual-01.mp4", section_id="s1",
                    source_preference="stock_still",
                )

            self.assertEqual(len(matches), 2)
            self.assertEqual({row["asset_id"] for _, row in matches}, {"p187", "x187"})
            self.assertEqual([row["source_actual"] for _, row in matches],
                             ["stock_still", "stock_still"])
            self.assertEqual([kind for kind, _ in accepted],
                             ["security", "transform", "security", "transform"])
            self.assertFalse(any(e["result"] == "recovery_failed" for e in source.events))

    def test_report_differentiates_candidate_preparation_failures_from_empty_search(self):
        failed = visual_qa._recovery_candidate_diagnostics([
            {"provider": "pexels", "result": "recovery_failed",
             "reason": "ai_still_render_format_unsupported"},
            {"provider": "pixabay", "result": "recovery_failed",
             "reason": "ai_still_render_format_unsupported"},
        ])
        self.assertEqual(failed["reason"], "alternate_candidates_rejected_before_visual_qa")
        self.assertEqual(len(failed["candidate_failures"]), 2)
        self.assertIn("ai_still_render_format_unsupported", failed["candidate_failures"][0]["detail"])
        empty = visual_qa._recovery_candidate_diagnostics([
            {"provider": "pexels_photo", "result": "empty", "reason": None}
        ])
        self.assertEqual(empty, {"reason": "alternate_search_returned_no_admitted_candidate"})

    def test_security_rejection_remains_visible_not_bypassed(self):
        diagnostics = visual_qa._recovery_candidate_diagnostics([
            {"provider": "pexels", "result": "recovery_security_blocked", "reason": "security_v1_block"}
        ])
        self.assertEqual(diagnostics["candidate_failures"][0]["result"], "recovery_security_blocked")


if __name__ == "__main__":
    unittest.main()
