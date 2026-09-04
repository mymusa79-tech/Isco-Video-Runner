from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_qc as qc


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _probe(*, seconds: float = 70.0, fmt: str = "film", hdr: bool = False) -> dict:
    width, height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    video = {
        "codec_type": "video",
        "codec_name": "h264",
        "width": width,
        "height": height,
        "pix_fmt": "yuv420p",
        "avg_frame_rate": "30/1",
        "color_transfer": "smpte2084" if hdr else "bt709",
        "color_primaries": "bt2020" if hdr else "bt709",
        "color_space": "bt2020nc" if hdr else "bt709",
    }
    audio = {
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": 2,
    }
    return {"streams": [video, audio], "format": {"duration": str(seconds)}}


class FinalMasterQCTests(unittest.TestCase):
    def _root(self, td: str, *, fmt: str = "film", body: float = 60.0) -> Path:
        root = Path(td)
        (root / "final.mp4").write_bytes(b"fake-final")
        _write_json(root / "plan.json", {"format": fmt})
        _write_json(
            root / "quality-final.json",
            {
                "format": fmt,
                "duration_ok": True,
                "audio_ok": True,
                "av_sync_ok": True,
                "duration_seconds": body,
                "video_stream_duration": body,
            },
        )
        # Long-form owns M7 visual-timeline. Moment intentionally does not: production
        # reaches its first Final Master QC before the Short finishing/cinematic seam.
        if fmt != "moment":
            _write_json(root / "visual-timeline.json", {"duration_seconds": body})
        return root

    def test_outro_black_silence_and_freeze_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {
                "returncode": 0,
                "black_events": [{"start_seconds": 61.0, "end_seconds": 70.0, "duration_seconds": 9.0}],
                "silence_events": [],
                "freeze_events": [],
                "stderr": "silence_start: 60.0\nsilence_end: 70.0 | silence_duration: 10.0\nfreeze_start: 60.0\nfreeze_duration: 10.0\nfreeze_end: 70.0\n",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["blocking_findings"], [])
            self.assertTrue(report["outro_excluded_from_perceptual_blocks"])

    def test_interior_near_black_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {
                "returncode": 0,
                "black_events": [{"start_seconds": 12.0, "end_seconds": 13.2, "duration_seconds": 1.2}],
                "silence_events": [], "freeze_events": [], "stderr": "",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "block")
            self.assertTrue(any(item.startswith("interior_near_black=") for item in report["blocking_findings"]))

    def test_interior_audio_dropout_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {
                "returncode": 0,
                "black_events": [], "silence_events": [], "freeze_events": [],
                "stderr": "silence_start: 10.0\nsilence_end: 15.5 | silence_duration: 5.5\n",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item.startswith("interior_audio_dropout=") for item in report["blocking_findings"]))

    def test_medium_exact_freeze_is_warning_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {
                "returncode": 0,
                "black_events": [], "silence_events": [], "freeze_events": [],
                "stderr": "freeze_start: 10.0\nfreeze_duration: 12.0\nfreeze_end: 22.0\n",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["status"], "pass")
            self.assertIn("interior_exact_freeze_observed_below_block_threshold", report["warnings"])
            self.assertEqual(len(report["detectors"]["exact_freeze"]["below_block_threshold"]), 1)

    def test_extreme_interior_exact_freeze_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {
                "returncode": 0,
                "black_events": [], "silence_events": [], "freeze_events": [],
                "stderr": "freeze_start: 10.0\nfreeze_duration: 32.0\nfreeze_end: 42.0\n",
            }
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item.startswith("interior_exact_freeze=") for item in report["blocking_findings"]))

    def test_decode_failure_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {"returncode": 1, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": "decode error"}
            with patch.object(qc, "probe", return_value=_probe()), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertFalse(report["full_decode_ok"])
            self.assertIn("full_decode_failed", report["blocking_findings"])

    def test_explicit_hdr_master_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            scan = {"returncode": 0, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": ""}
            with patch.object(qc, "probe", return_value=_probe(hdr=True)), patch.object(qc, "_run_full_scan", return_value=scan):
                with self.assertRaises(qc.FinalMasterQCError):
                    qc.run_final_master_qc(root)
            report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertTrue(any(item.startswith("unexpected_hdr_master=") for item in report["blocking_findings"]))

    def test_missing_color_tags_warn_but_do_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td)
            info = _probe()
            video = info["streams"][0]
            video.pop("color_transfer")
            video.pop("color_primaries")
            video.pop("color_space")
            scan = {"returncode": 0, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": ""}
            with patch.object(qc, "probe", return_value=info), patch.object(qc, "_run_full_scan", return_value=scan):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["status"], "pass")
            self.assertIn("color_tags_incomplete_but_no_explicit_hdr_tag", report["warnings"])

    def test_portrait_moment_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._root(td, fmt="moment", body=15.0)
            self.assertFalse((root / "visual-timeline.json").exists())
            scan = {"returncode": 0, "black_events": [], "silence_events": [], "freeze_events": [], "stderr": ""}
            with patch.object(qc, "probe", return_value=_probe(seconds=15.0, fmt="moment")), patch.object(qc, "_run_full_scan", return_value=scan):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["stream_contract"]["width"], 1080)
            self.assertEqual(report["stream_contract"]["height"], 1920)
            self.assertEqual(report["body_contract_kind"], "moment_measured_render")


if __name__ == "__main__":
    unittest.main()