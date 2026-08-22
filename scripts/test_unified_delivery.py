from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.unified_delivery import build_delivery_manifest


class UnifiedDeliveryTests(unittest.TestCase):
    def _root(self, *, fmt: str = "film", candidates: int = 3) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "final.mp4").write_bytes(b"x" * 2048)
        (root / "plan.json").write_text(json.dumps({"topic": "موضوع", "format": fmt}), encoding="utf-8")
        (root / "quality-final.json").write_text(json.dumps({"format": fmt}), encoding="utf-8")
        (root / "production-manifest.json").write_text(json.dumps({"format": fmt}), encoding="utf-8")
        (root / "rights-manifest.json").write_text("{}", encoding="utf-8")
        (root / "gold-enforce-report.json").write_text("{}", encoding="utf-8")
        payload = {
            "candidates": [
                {
                    "experiment_slot": chr(ord("A") + index),
                    "title_ar": f"عنوان {index + 1}",
                    "file": f"thumbnail-{index + 1}.jpg",
                    "text_ar": f"نص {index + 1}",
                    "packaging_hypothesis": f"فرضية {index + 1}",
                }
                for index in range(candidates)
            ]
        }
        (root / "thumbnail-plan.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return root

    def test_long_delivery_exposes_one_release_url_and_three_pairs(self):
        manifest = build_delivery_manifest(
            self._root(), repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-123"
        )
        self.assertEqual(manifest["delivery_kind"], "long")
        self.assertEqual(len(manifest["title_thumbnail_pairs"]), 3)
        self.assertEqual(manifest["title_thumbnail_pairs"][0]["slot"], "A")
        self.assertEqual(manifest["youtube_publish_mode"], "manual_in_youtube_studio")
        self.assertFalse(manifest["publication_performed"])
        self.assertTrue(manifest["delivery_url"].endswith("/releases/tag/video-123"))

    def test_long_delivery_blocks_partial_abc(self):
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            build_delivery_manifest(
                self._root(candidates=2), repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-124"
            )

    def test_short_delivery_allows_its_own_release(self):
        manifest = build_delivery_manifest(
            self._root(fmt="moment", candidates=0),
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag="short-88",
        )
        self.assertEqual(manifest["delivery_kind"], "short")
        self.assertEqual(manifest["title_thumbnail_pairs"], [])
        self.assertFalse(manifest["publication_performed"])

    def test_request_summary_does_not_copy_entire_private_state(self):
        request = {
            "request_id": "req-1",
            "request_sha256": "abc",
            "approval_scope": "long_plus_sibling_shorts",
            "approved_topic": "موضوع",
            "approved_at": "2026-08-22T00:00:00Z",
            "candidate": {"evidence": ["large private state"]},
        }
        manifest = build_delivery_manifest(
            self._root(), repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-125", request=request
        )
        self.assertNotIn("candidate", manifest["control_request"])
        self.assertEqual(manifest["control_request"]["request_id"], "req-1")


if __name__ == "__main__":
    unittest.main()
