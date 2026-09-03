from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_acceptance_v2 as acceptance
from scripts.final_master_acceptance_v2 import (
    FinalMasterAcceptanceError,
    require_certified_final_video,
    require_final_master_acceptance,
    seal_final_master_acceptance,
)


def _json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _root(td: str, *, fmt: str = "film") -> Path:
    root = Path(td)
    (root / "final.mp4").write_bytes(b"final-bytes" * 500)
    _json(root / "plan.json", {"format": fmt, "topic": "x"})
    _json(root / "quality-final.json", {"format": fmt, "status": "pass"})
    _json(root / "visual-timeline.json", {"duration_seconds": 12.0})
    return root


def _probe(*, profile: str = "High") -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": profile,
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


def _core_pass(fmt: str) -> dict:
    return {
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


def _seal(root: Path, *, fmt: str = "film", profile: str = "High") -> dict:
    with patch.object(acceptance, "probe", return_value=_probe(profile=profile)), patch.object(
        acceptance, "_mp4_fast_start", return_value=(True, ["ftyp", "moov", "mdat"])
    ):
        return seal_final_master_acceptance(root, _core_pass(fmt))


class FinalMasterAcceptanceV2Tests(unittest.TestCase):
    def test_exact_receipt_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            sealed = _seal(root)
            accepted = require_final_master_acceptance(root)
            self.assertEqual(accepted, sealed)
            contract = accepted["acceptance_contract"]
            self.assertEqual(contract["contract_id"], "final.master.acceptance.v2")
            self.assertEqual(contract["decision"], "pass")
            self.assertEqual(len(contract["qc_policy_fingerprint"]), 64)
            self.assertEqual(len(contract["implementation_sha256"]), 64)
            self.assertEqual(accepted["upload_conformance"]["decision"], "pass")

    def test_final_byte_mutation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            _seal(root)
            (root / "final.mp4").write_bytes(b"changed" * 800)
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "artifact_identity_mismatch"):
                require_final_master_acceptance(root)

    def test_plan_mutation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            _seal(root)
            _json(root / "plan.json", {"format": "film", "topic": "changed"})
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "artifact_identity_mismatch"):
                require_final_master_acceptance(root)

    def test_quality_mutation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            _seal(root)
            _json(root / "quality-final.json", {"format": "film", "status": "changed"})
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "artifact_identity_mismatch"):
                require_final_master_acceptance(root)

    def test_timeline_mutation_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            _seal(root)
            _json(root / "visual-timeline.json", {"duration_seconds": 13.0})
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "artifact_identity_mismatch"):
                require_final_master_acceptance(root)

    def test_symlink_final_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            target = root / "target.mp4"
            target.write_bytes((root / "final.mp4").read_bytes())
            (root / "final.mp4").unlink()
            try:
                (root / "final.mp4").symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "invalid_file"):
                _seal(root)

    def test_explicit_upload_metadata_conflict_blocks_after_core_pass(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td)
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "upload_conformance_block"):
                _seal(root, profile="Main")
            document = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(document["status"], "block")
            self.assertIn("unexpected_h264_profile=Main", document["blocking_findings"])

    def test_staged_renamed_short_accepts_same_bytes_and_rejects_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td, fmt="moment")
            _seal(root, fmt="moment")
            staged = root / "short-01.mp4"
            staged.write_bytes((root / "final.mp4").read_bytes())
            require_certified_final_video(root / "final-master-qc.json", staged)
            staged.write_bytes(b"tampered" * 700)
            with self.assertRaisesRegex(FinalMasterAcceptanceError, "final_video_mismatch"):
                require_certified_final_video(root / "final-master-qc.json", staged)


if __name__ == "__main__":
    unittest.main()
