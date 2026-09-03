from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import final_master_acceptance_v2 as acceptance
from scripts import final_qc_observer_durability as durability
from scripts.final_master_acceptance_v2 import FinalMasterAcceptanceError


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _probe(fmt: str = "film", *, profile: str = "High") -> dict:
    width, height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": profile,
                "field_order": "progressive",
                "width": width,
                "height": height,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
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
        ],
        "format": {"duration": "12.0"},
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


def _root(td: str, fmt: str) -> Path:
    root = Path(td)
    (root / "final.mp4").write_bytes(b"master" * 1000)
    _write(root / "plan.json", {"format": fmt})
    _write(root / "quality-final.json", {"format": fmt})
    _write(root / "visual-timeline.json", {"duration_seconds": 11.5})
    return root


class FinalMasterQCContractShapeTests(unittest.TestCase):
    def test_runtime_wrapper_seals_exact_artifact_contract_for_long_and_short(self) -> None:
        for fmt in ("film", "moment"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as td:
                root = _root(td, fmt)
                original = Mock(return_value=_core_pass(fmt))
                with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": ""}, clear=False), patch.object(
                    acceptance, "probe", return_value=_probe(fmt)
                ), patch.object(
                    acceptance, "_mp4_fast_start", return_value=(True, ["ftyp", "moov", "mdat"])
                ):
                    report = durability.run_final_master_qc_durable(root, original=original)
                original.assert_called_once_with(root)
                contract = report["acceptance_contract"]
                self.assertEqual(contract["contract_id"], "final.master.acceptance.v2")
                self.assertEqual(contract["decision"], "pass")
                self.assertEqual(len(contract["sources"]["final"]["sha256"]), 64)
                self.assertEqual(
                    contract["sources"]["final"]["byte_length"],
                    (root / "final.mp4").stat().st_size,
                )
                self.assertEqual(report["upload_conformance"]["decision"], "pass")
                self.assertTrue((root / "final-master-qc.json").is_file())

    def test_explicit_wrong_upload_metadata_blocks_above_core(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _root(td, "film")
            original = Mock(return_value=_core_pass("film"))
            with patch.dict(os.environ, {"ISCO_TTS_CACHE_PATH": ""}, clear=False), patch.object(
                acceptance, "probe", return_value=_probe("film", profile="Main")
            ), patch.object(
                acceptance, "_mp4_fast_start", return_value=(True, ["ftyp", "moov", "mdat"])
            ):
                with self.assertRaisesRegex(FinalMasterAcceptanceError, "upload_conformance_block"):
                    durability.run_final_master_qc_durable(root, original=original)
            original.assert_called_once_with(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "block")
            self.assertIn("unexpected_h264_profile=Main", report["blocking_findings"])


if __name__ == "__main__":
    unittest.main()
