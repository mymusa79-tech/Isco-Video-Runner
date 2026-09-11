from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import qc_pending_resume_bundle_v1 as bundle


RUNNER_SHA = "1" * 40
ENGINE_SHA = "2" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint(root: Path, fmt: str) -> dict:
    return {
        "runner_sha": RUNNER_SHA,
        "engine_sha": ENGINE_SHA,
        "source_run_id": "36362015270",
        "source_run_attempt": "1",
        "format": fmt,
        "final": {"sha256": _sha256(root / "final.mp4")},
    }


def _seed_source(root: Path, fmt: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    required = bundle.COMMON_REQUIRED_FILES + (
        bundle.SHORT_REQUIRED_FILES if fmt == "moment" else ()
    )
    for name in required:
        (root / name).write_bytes(f"seed:{fmt}:{name}".encode("utf-8"))

    # Reproduce the Run #241 dependency shape: Final Master binds two audio evidence
    # files, one of which was absent from the historical bundle allow-list.
    for name in ("audio-production-contract-v2.json", "audio-producer-repair.json"):
        (root / name).write_text(json.dumps({"file": name}), encoding="utf-8")

    final_master = {
        "acceptance_contract": {
            "upstream_evidence": {
                "audio_production_contract": {
                    "file": "audio-production-contract-v2.json",
                },
                "audio_producer_repair": {
                    "file": "audio-producer-repair.json",
                },
            }
        }
    }
    (root / "final-master-qc.json").write_text(
        json.dumps(final_master), encoding="utf-8"
    )
    return _checkpoint(root, fmt)


class QcPendingRun241ClosureTests(unittest.TestCase):
    def test_final_master_receipt_drives_dynamic_evidence_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_source(root, "moment")
            self.assertEqual(
                bundle._final_master_upstream_evidence_files(root),
                (
                    "audio-production-contract-v2.json",
                    "audio-producer-repair.json",
                ),
            )

    def test_receipt_file_binding_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "final-master-qc.json").write_text(
                json.dumps(
                    {
                        "acceptance_contract": {
                            "upstream_evidence": {
                                "bad": {"file": "../outside.json"}
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unsafe file binding"):
                bundle._final_master_upstream_evidence_files(root)

    def test_run241_bundle_shape_closes_for_film_story_and_moment(self) -> None:
        for fmt in ("film", "story", "moment"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source"
                destination = source / "short-circuit-gold-resume-v2"
                checkpoint = _seed_source(source, fmt)

                def validate_staging(path: Path, **_: object) -> dict:
                    staged = Path(path)
                    self.assertTrue((staged / "audio-producer-repair.json").is_file())
                    manifest = json.loads(
                        (staged / bundle.MANIFEST_FILENAME).read_text(encoding="utf-8")
                    )
                    self.assertTrue(
                        manifest["files"]["audio-producer-repair.json"]["required"]
                    )
                    self.assertEqual(
                        manifest["final_master_upstream_evidence_files"],
                        [
                            "audio-production-contract-v2.json",
                            "audio-producer-repair.json",
                        ],
                    )
                    return manifest

                with (
                    patch.object(
                        bundle,
                        "_verify_qc_pending_checkpoint",
                        return_value=checkpoint,
                    ),
                    patch.object(bundle, "validate_resume_bundle", side_effect=validate_staging),
                ):
                    bundle.build_resume_bundle(source, destination)

                self.assertTrue((destination / "audio-producer-repair.json").is_file())
                self.assertFalse(
                    any(source.glob(f".{destination.name}.staging-*")),
                    "validated promotion must not leave staging directories",
                )

    def test_failed_validation_never_replaces_existing_recovery_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = source / "short-circuit-gold-resume-v2"
            checkpoint = _seed_source(source, "moment")
            destination.mkdir()
            marker = destination / "known-good-marker"
            marker.write_text("keep", encoding="utf-8")

            with (
                patch.object(
                    bundle,
                    "_verify_qc_pending_checkpoint",
                    return_value=checkpoint,
                ),
                patch.object(
                    bundle,
                    "validate_resume_bundle",
                    side_effect=RuntimeError("synthetic validation failure"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic validation failure"):
                    bundle.build_resume_bundle(source, destination)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse(
                any(source.glob(f".{destination.name}.staging-*")),
                "failed build must clean unpublished staging directories",
            )


if __name__ == "__main__":
    unittest.main()
