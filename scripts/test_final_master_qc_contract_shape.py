from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_qc as qc


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _probe(fmt: str = "film") -> dict:
    width, height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
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


class FinalMasterQCContractShapeTests(unittest.TestCase):
    def test_qc_writes_exact_artifact_contract_for_long_and_short(self) -> None:
        for fmt in ("film", "moment"):
            with self.subTest(fmt=fmt), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "final.mp4").write_bytes(b"master" * 1000)
                _write(root / "plan.json", {"format": fmt})
                _write(root / "quality-final.json", {"format": fmt})
                _write(root / "visual-timeline.json", {"duration_seconds": 11.5})
                scan = {"returncode": 0, "timed_out": False, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": ""}
                with patch.object(qc, "probe", return_value=_probe(fmt)), patch.object(qc, "_run_full_scan", return_value=scan):
                    report = qc.run_final_master_qc(root)
                acceptance = report["acceptance_contract"]
                self.assertEqual(acceptance["contract_id"], "final.master.acceptance.v2")
                self.assertEqual(acceptance["decision"], "pass")
                self.assertEqual(len(acceptance["sources"]["final"]["sha256"]), 64)
                self.assertEqual(acceptance["sources"]["final"]["byte_length"], (root / "final.mp4").stat().st_size)
                self.assertTrue((root / "final-master-qc.json").is_file())

    def test_explicit_wrong_upload_metadata_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"master" * 1000)
            _write(root / "plan.json", {"format": "film"})
            _write(root / "quality-final.json", {"format": "film"})
            _write(root / "visual-timeline.json", {"duration_seconds": 11.5})
            info = _probe("film")
            info["streams"][0]["profile"] = "Main"
            scan = {"returncode": 0, "timed_out": False, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": ""}
            with patch.object(qc, "probe", return_value=info), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertIn("unexpected_h264_profile=Main", report["blocking_findings"])


if __name__ == "__main__":
    unittest.main()
