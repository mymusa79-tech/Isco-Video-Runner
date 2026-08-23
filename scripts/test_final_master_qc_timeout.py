from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_qc as qc


def _probe() -> dict:
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "color_space": "bt709",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
        "format": {"duration": "70.0"},
    }


class FinalMasterQCTimeoutTests(unittest.TestCase):
    def test_scan_passes_explicit_timeout_and_converts_timeout_to_fail_closed_result(self) -> None:
        expired = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=qc.FULL_SCAN_TIMEOUT_SECONDS, stderr="partial")
        with patch.object(qc.subprocess, "run", side_effect=expired) as run:
            scan = qc._run_full_scan(Path("final.mp4"), has_audio=True)
        self.assertTrue(scan["timed_out"])
        self.assertEqual(scan["returncode"], 124)
        self.assertEqual(run.call_args.kwargs["timeout"], qc.FULL_SCAN_TIMEOUT_SECONDS)

    def test_timeout_writes_blocking_evidence_before_raising(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"fake")
            (root / "plan.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
            (root / "quality-final.json").write_text(json.dumps({"format": "film"}), encoding="utf-8")
            (root / "visual-timeline.json").write_text(json.dumps({"duration_seconds": 60.0}), encoding="utf-8")
            scan = {
                "returncode": 124,
                "timed_out": True,
                "black_events": [],
                "silence_events": [],
                "freeze_events": [],
                "stderr": "",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "block")
            self.assertTrue(report["full_decode_timed_out"])
            self.assertFalse(report["full_decode_ok"])
            self.assertEqual(report["full_decode_timeout_seconds"], qc.FULL_SCAN_TIMEOUT_SECONDS)
            self.assertIn("full_decode_timeout", report["blocking_findings"])


if __name__ == "__main__":
    unittest.main()
