from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import run184_qr_confirmation_closure as qr
from scripts import run184_qr_confirmation_runtime as qr_runtime
from scripts.qr_runtime_bootstrap import ensure_qr_confirmation_runtime


class _Firewall:
    def __init__(self, codes_by_name: dict[str, tuple[str, ...]]):
        self.codes_by_name = dict(codes_by_name)

    def scan_frame(self, frame: Path):
        codes = self.codes_by_name.get(Path(frame).name, ())
        return SimpleNamespace(
            detections=tuple(SimpleNamespace(code=code) for code in codes),
            safe_for_normal_vision=not bool(codes),
        )


class Run184QRConfirmationClosureTests(unittest.TestCase):
    def test_distributed_video_samples_cover_beginning_middle_and_end(self) -> None:
        self.assertEqual(qr._video_sample_times(10.0), (1.2, 5.0, 8.8))
        short = qr._video_sample_times(0.1)
        self.assertTrue(short)
        self.assertTrue(all(0.0 <= value <= 0.05 for value in short))

    def test_one_frame_engine_geometry_suspicion_cannot_block_video(self) -> None:
        frames = (Path("a.pgm"), Path("b.pgm"), Path("c.pgm"))
        firewall = _Firewall({"b.pgm": ("qr_code_detected",)})
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", return_value="none"
        ), patch.object(qr, "confirm_qr_finder_geometry", return_value=True):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_even_repeated_legacy_geometry_cannot_block_without_mature_detection(self) -> None:
        frames = (Path("a.pgm"), Path("b.pgm"), Path("c.pgm"))
        firewall = _Firewall(
            {
                "a.pgm": ("qr_code_detected",),
                "b.pgm": ("qr_code_detected",),
                "c.pgm": ("qr_code_detected",),
            }
        )
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", return_value="none"
        ), patch.object(qr, "confirm_qr_finder_geometry", return_value=True):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_zbar_valid_decode_is_immediate_hard_block(self) -> None:
        frames = (Path("a.pgm"), Path("b.pgm"), Path("c.pgm"))
        firewall = _Firewall({})
        with patch.object(qr, "_zbar_decodes_qr", side_effect=[False, True, False]), patch.object(
            qr, "_zxing_qr_status", return_value="none"
        ), self.assertRaisesRegex(RuntimeError, "qr_code_detected"):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_zxing_valid_decode_is_immediate_hard_block(self) -> None:
        frames = (Path("a.pgm"),)
        firewall = _Firewall({})
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", return_value="decoded"
        ), self.assertRaisesRegex(RuntimeError, "qr_code_detected"):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_one_undecodable_mature_detection_does_not_block_video(self) -> None:
        frames = (Path("a.pgm"), Path("b.pgm"), Path("c.pgm"))
        firewall = _Firewall({})
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", side_effect=["none", "detected_error", "none"]
        ):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_two_distributed_undecodable_mature_detections_block_video(self) -> None:
        frames = (Path("a.pgm"), Path("b.pgm"), Path("c.pgm"))
        firewall = _Firewall({})
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", side_effect=["detected_error", "none", "detected_error"]
        ), self.assertRaisesRegex(RuntimeError, "qr_code_detected"):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_one_undecodable_mature_detection_blocks_still_image(self) -> None:
        frames = (Path("still.pgm"),)
        firewall = _Firewall({})
        with patch.object(qr, "_zbar_decodes_qr", return_value=False), patch.object(
            qr, "_zxing_qr_status", return_value="detected_error"
        ), self.assertRaisesRegex(RuntimeError, "qr_code_detected"):
            qr._evaluate_frames(
                frames,
                video=False,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )

    def test_non_qr_security_findings_remain_authoritative(self) -> None:
        frames = (Path("a.pgm"),)
        firewall = _Firewall({"a.pgm": ("qr_code_detected", "prompt_like_text_detected")})
        with patch.object(qr, "_zbar_decodes_qr") as zbar, self.assertRaisesRegex(
            RuntimeError, "prompt_like_text_detected"
        ):
            qr._evaluate_frames(
                frames,
                video=True,
                zbar="zbarimg",
                zxing="ZXingReader",
                firewall=firewall,
            )
        zbar.assert_not_called()

    def test_confirmation_runtime_missing_is_fail_closed_infrastructure(self) -> None:
        resolved = {
            "ffmpeg": "/usr/bin/ffmpeg",
            "ffprobe": "/usr/bin/ffprobe",
            "tesseract": "/usr/bin/tesseract",
            "zbarimg": None,
            "ZXingReader": "/usr/bin/ZXingReader",
        }
        with patch.object(qr.shutil, "which", side_effect=lambda name: resolved.get(name)):
            with self.assertRaisesRegex(qr.QRConfirmationInfrastructureError, "qr_confirmation_runtime_unavailable"):
                qr._required_runtime()

    def test_zxing_noble_221_cli_contract(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='frame.pgm QRCode "payload"\n', stderr=""
        )
        with patch.object(qr_runtime.subprocess, "run", return_value=completed) as run:
            self.assertEqual(
                qr_runtime._zxing_qr_status_221(Path("frame.pgm"), "ZXingReader"),
                "decoded",
            )
        argv = run.call_args.args[0]
        self.assertIn("-format", argv)
        self.assertIn("-errors", argv)
        self.assertIn("-1", argv)
        self.assertNotIn("-formats", argv)
        self.assertNotIn("-single", argv)

    def test_zxing_noble_221_nonzero_barcode_error_is_detection_not_infra_failure(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="frame.pgm QRCode ChecksumError\n", stderr=""
        )
        with patch.object(qr_runtime.subprocess, "run", return_value=completed):
            self.assertEqual(
                qr_runtime._zxing_qr_status_221(Path("frame.pgm"), "ZXingReader"),
                "detected_error",
            )

    def test_zxing_noble_221_none_is_clean_scan(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="frame.pgm None\n", stderr=""
        )
        with patch.object(qr_runtime.subprocess, "run", return_value=completed):
            self.assertEqual(
                qr_runtime._zxing_qr_status_221(Path("frame.pgm"), "ZXingReader"),
                "none",
            )

    def test_real_zbar_and_zxing_smoke_on_generated_qr_and_blank_frame(self) -> None:
        tools = ensure_qr_confirmation_runtime(allow_install=True)
        resolved = {
            "ffmpeg": shutil.which("ffmpeg"),
            "zbarimg": tools.zbarimg,
            "ZXingReader": tools.zxing_reader,
            "ZXingWriter": shutil.which("ZXingWriter"),
        }
        missing = [name for name, path in resolved.items() if not path]
        self.assertFalse(missing, f"mandatory QR confirmation tools missing: {missing}")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qr_png = root / "qr.png"
            qr_pgm = root / "qr.pgm"
            blank_pgm = root / "blank.pgm"

            subprocess.run(
                [resolved["ZXingWriter"], "QRCode", "isco-run184-smoke", str(qr_png)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            subprocess.run(
                [
                    resolved["ffmpeg"],
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(qr_png),
                    "-vf",
                    "scale=640:-2:flags=neighbor,format=gray",
                    str(qr_pgm),
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
            )
            blank_pgm.write_bytes(b"P5\n128 128\n255\n" + bytes([255]) * (128 * 128))

            self.assertTrue(qr._zbar_decodes_qr(qr_pgm, resolved["zbarimg"]))
            self.assertEqual(
                qr_runtime._zxing_qr_status_221(qr_pgm, resolved["ZXingReader"]),
                "decoded",
            )
            self.assertFalse(qr._zbar_decodes_qr(blank_pgm, resolved["zbarimg"]))
            self.assertEqual(
                qr_runtime._zxing_qr_status_221(blank_pgm, resolved["ZXingReader"]),
                "none",
            )


if __name__ == "__main__":
    unittest.main()
