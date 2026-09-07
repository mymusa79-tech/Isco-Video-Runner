from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.delivery_acceptance_v2 import seal_delivery_acceptance
from scripts.unified_delivery import build_delivery_manifest, finalize_release_manifest


class ReleaseCandidateIdentityClosureTests(unittest.TestCase):
    def test_build_rejects_production_delivery_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
            (root / "quality-final.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
            (root / "production-manifest.json").write_text(
                json.dumps({"format": "film", "release_tag": "telegram-req-abc"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Release candidate identity mismatch"):
                build_delivery_manifest(
                    root,
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-999",
                )

    def test_finalize_rejects_existing_delivery_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "delivery-manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_state": "staged",
                        "release_tag": None,
                        "delivery_url": None,
                        "release_candidate_tag": "telegram-req-abc",
                        "publication_performed": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "release candidate changed"):
                finalize_release_manifest(
                    path,
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-999",
                )

    def test_finalize_rejects_production_candidate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "production-manifest.json").write_text(
                json.dumps({"release_tag": "telegram-req-abc"}),
                encoding="utf-8",
            )
            path = root / "delivery-manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_state": "staged",
                        "release_tag": None,
                        "delivery_url": None,
                        "release_candidate_tag": None,
                        "publication_performed": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "Release candidate identity mismatch"):
                finalize_release_manifest(
                    path,
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-999",
                )

    def test_terminal_acceptance_rejects_candidate_release_drift_before_receipt_read(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            delivery = root / "delivery-manifest.json"
            delivery.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_state": "staged",
                        "release_tag": None,
                        "delivery_url": None,
                        "release_candidate_tag": "telegram-req-abc",
                        "primary_video_sha256": "a" * 64,
                        "final_master_qc": {
                            "file": "final-master-qc.json",
                            "file_size": 1,
                            "file_sha256": "b" * 64,
                        },
                        "youtube_publish_mode": "manual_in_youtube_studio",
                        "publication_performed": False,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "candidate identity does not match"):
                seal_delivery_acceptance(
                    delivery_manifest=delivery,
                    release_receipt=root / "missing-release-receipt.json",
                    release_journal=root / "missing-release-journal.json",
                    repository="mymusa79-tech/Isco-Video-Runner",
                    release_tag="video-999",
                    target_sha="c" * 40,
                    output=root / "terminal.json",
                )

    def test_legacy_unbound_candidate_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            path = root / "delivery-manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "release_state": "staged",
                        "release_tag": None,
                        "delivery_url": None,
                        "release_candidate_tag": None,
                        "publication_performed": False,
                    }
                ),
                encoding="utf-8",
            )
            finalize_release_manifest(
                path,
                repository="mymusa79-tech/Isco-Video-Runner",
                release_tag="video-42",
            )
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["release_state"], "staged")
            self.assertEqual(manifest["release_candidate_tag"], "video-42")
            self.assertIsNone(manifest["release_tag"])


if __name__ == "__main__":
    unittest.main()
