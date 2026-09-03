from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_acceptance_v2 as acceptance
from scripts.unified_delivery import build_delivery_manifest, finalize_release_manifest, write_delivery_manifest


def _probe() -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "field_order": "progressive",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "color_space": "bt709",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_rate": "48000",
                "channels": 2,
            },
        ]
    }


def _seal_qc(root: Path, *, fmt: str) -> dict:
    report = {
        "schema_version": 1,
        "status": "pass",
        "production_stage": "post_render_pre_gold_acceptance",
        "format": fmt,
        "full_decode_ok": True,
        "full_decode_timed_out": False,
        "final_media_mutated": False,
        "blocking_findings": [],
        "warnings": [],
    }
    with patch.object(acceptance, "probe", return_value=_probe()), patch.object(
        acceptance, "_mp4_fast_start", return_value=(True, ["ftyp", "moov", "mdat"])
    ):
        return acceptance.seal_final_master_acceptance(root, report)


class UnifiedDeliveryTests(unittest.TestCase):
    def _root(self, *, fmt: str = "film", candidates: int = 3) -> Path:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "final.mp4").write_bytes(b"x" * 2048)
        (root / "plan.json").write_text(json.dumps({"topic": "موضوع", "format": fmt}), encoding="utf-8")
        (root / "quality-final.json").write_text(json.dumps({"format": fmt}), encoding="utf-8")
        (root / "visual-timeline.json").write_text(json.dumps({"duration_seconds": 12.0}), encoding="utf-8")
        (root / "production-manifest.json").write_text(json.dumps({"format": fmt}), encoding="utf-8")
        (root / "rights-manifest.json").write_text("{}", encoding="utf-8")
        (root / "gold-enforce-report.json").write_text("{}", encoding="utf-8")
        _seal_qc(root, fmt=fmt)
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
        for index in range(candidates):
            (root / f"thumbnail-{index + 1}.jpg").write_bytes(b"j" * 2048)
        return root

    def _short_assets(self, root: Path, count: int = 3) -> list[dict]:
        assets = []
        for index in range(1, count + 1):
            child = root / f"child-{index:02d}"
            child.mkdir()
            (child / "final.mp4").write_bytes(bytes([64 + index]) * 2048)
            (child / "plan.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
            (child / "quality-final.json").write_text(json.dumps({"format": "moment"}), encoding="utf-8")
            (child / "visual-timeline.json").write_text(json.dumps({"duration_seconds": 12.0}), encoding="utf-8")
            _seal_qc(child, fmt="moment")

            name = f"short-{index:02d}.mp4"
            qc_name = f"short-{index:02d}-master-qc.json"
            shutil.copy2(child / "final.mp4", root / name)
            shutil.copy2(child / "final-master-qc.json", root / qc_name)
            assets.append(
                {
                    "slot": f"S{index}",
                    "semantic_job": f"زاوية {index}",
                    "request_id": f"req-s{index}",
                    "request_sha256": f"sha-{index}",
                    "video": name,
                    "final_master_qc": qc_name,
                    "delivery_allowed": True,
                }
            )
        return assets

    def test_release_tag_is_candidate_only_before_remote_transaction(self):
        manifest = build_delivery_manifest(
            self._root(), repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-123"
        )
        self.assertEqual(manifest["delivery_kind"], "long")
        self.assertEqual(len(manifest["title_thumbnail_pairs"]), 3)
        self.assertEqual(manifest["title_thumbnail_pairs"][0]["slot"], "A")
        self.assertEqual(manifest["youtube_publish_mode"], "manual_in_youtube_studio")
        self.assertFalse(manifest["publication_performed"])
        self.assertEqual(manifest["release_state"], "staged")
        self.assertIsNone(manifest["release_tag"])
        self.assertIsNone(manifest["delivery_url"])
        self.assertEqual(manifest["release_candidate_tag"], "video-123")
        self.assertTrue(manifest["release_candidate_url"].endswith("/releases/tag/video-123"))
        self.assertEqual(manifest["final_master_qc"]["evidence"]["status"], "pass")
        self.assertEqual(
            manifest["primary_video_sha256"],
            manifest["final_master_qc"]["evidence"]["acceptance_contract"]["sources"]["final"]["sha256"],
        )

    def test_staged_long_plus_shorts_is_complete_before_release_tag_exists(self):
        root = self._root()
        request = {"request_id": "req-1", "request_sha256": "abc", "approval_scope": "long_plus_sibling_shorts", "approved_topic": "موضوع"}
        manifest = build_delivery_manifest(
            root,
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag=None,
            request=request,
            short_assets=self._short_assets(root, 3),
        )
        self.assertEqual(manifest["delivery_kind"], "long_plus_shorts")
        self.assertEqual(manifest["release_state"], "staged")
        self.assertIsNone(manifest["delivery_url"])
        self.assertEqual(manifest["short_count"], 3)
        self.assertFalse(manifest["partial_delivery_allowed"])
        self.assertTrue(all(item["final_master_qc"]["evidence"]["status"] == "pass" for item in manifest["shorts"]))

    def test_primary_video_mutation_after_p4_blocks_delivery(self):
        root = self._root()
        (root / "final.mp4").write_bytes(b"mutated" * 500)
        with self.assertRaisesRegex(RuntimeError, "exact current Final Master acceptance"):
            build_delivery_manifest(root, repository="r/x", release_tag=None)

    def test_sibling_video_mutation_after_p4_blocks_delivery(self):
        root = self._root()
        shorts = self._short_assets(root, 2)
        (root / shorts[0]["video"]).write_bytes(b"mutated-short" * 300)
        with self.assertRaisesRegex(RuntimeError, "exact Final Master acceptance"):
            build_delivery_manifest(root, repository="r/x", release_tag=None, short_assets=shorts)

    def test_bundle_request_blocks_partial_short_delivery(self):
        root = self._root()
        request = {"request_id": "req-1", "request_sha256": "abc", "approval_scope": "long_plus_sibling_shorts", "approved_topic": "موضوع"}
        with self.assertRaisesRegex(RuntimeError, "2–3"):
            build_delivery_manifest(
                root,
                repository="mymusa79-tech/Isco-Video-Runner",
                release_tag=None,
                request=request,
                short_assets=self._short_assets(root, 1),
            )

    def test_long_delivery_blocks_partial_abc(self):
        with self.assertRaisesRegex(RuntimeError, "exactly three"):
            build_delivery_manifest(
                self._root(candidates=2), repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-124"
            )

    def test_short_delivery_allows_its_own_release_candidate(self):
        manifest = build_delivery_manifest(
            self._root(fmt="moment", candidates=0),
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag="short-88",
        )
        self.assertEqual(manifest["delivery_kind"], "short")
        self.assertEqual(manifest["title_thumbnail_pairs"], [])
        self.assertFalse(manifest["publication_performed"])
        self.assertEqual(manifest["release_state"], "staged")
        self.assertEqual(manifest["release_candidate_tag"], "short-88")

    def test_legacy_finalize_call_only_binds_candidate_not_released_truth(self):
        root = self._root()
        path = write_delivery_manifest(
            root,
            repository="mymusa79-tech/Isco-Video-Runner",
            release_tag=None,
        )
        finalize_release_manifest(path, repository="mymusa79-tech/Isco-Video-Runner", release_tag="video-999")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["release_state"], "staged")
        self.assertIsNone(manifest["release_tag"])
        self.assertIsNone(manifest["delivery_url"])
        self.assertEqual(manifest["release_candidate_tag"], "video-999")
        self.assertTrue(manifest["release_candidate_url"].endswith("/video-999"))
        self.assertFalse(manifest["publication_performed"])
        self.assertEqual(manifest["youtube_publish_mode"], "manual_in_youtube_studio")

    def test_request_summary_does_not_copy_entire_private_state(self):
        request = {
            "request_id": "req-1",
            "request_sha256": "abc",
            "approval_scope": "long_only",
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
