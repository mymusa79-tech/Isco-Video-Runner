from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_unchanged_legacy_qc():
    def probe(path: Path) -> dict:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return json.loads(proc.stdout)

    def secret_free_subprocess_env() -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not any(token in key.casefold() for token in ("secret", "token", "key"))
        }

    package = types.ModuleType("isco_video_agent")
    package.__path__ = []
    media = types.ModuleType("isco_video_agent.media")
    media.__path__ = []
    ffmpeg = types.ModuleType("isco_video_agent.media.ffmpeg")
    ffmpeg.probe = probe
    security = types.ModuleType("isco_video_agent.security")
    security.secret_free_subprocess_env = secret_free_subprocess_env

    modules = {
        "isco_video_agent": package,
        "isco_video_agent.media": media,
        "isco_video_agent.media.ffmpeg": ffmpeg,
        "isco_video_agent.security": security,
    }
    spec = importlib.util.spec_from_file_location(
        "clean_v2_legacy_final_master_qc_test",
        Path("scripts/final_master_qc.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load legacy Final Master QC")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


def _write_support_files(root: Path, *, duration: float) -> None:
    (root / "plan.json").write_text(
        json.dumps({"title": "fixture"}), encoding="utf-8"
    )
    (root / "quality-final.json").write_text(
        json.dumps({"format": "film"}), encoding="utf-8"
    )
    (root / "visual-timeline.json").write_text(
        json.dumps({"duration_seconds": duration}), encoding="utf-8"
    )


def _render_fixture(path: Path, *, width: int, height: int, duration: float) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate=30:duration={duration}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            str(path),
        ],
        check=True,
        timeout=120,
    )


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe required",
)
class CleanV2LegacyFinalMasterCoreTests(unittest.TestCase):
    def test_real_clean_master_passes_unchanged_legacy_core(self) -> None:
        qc = _load_unchanged_legacy_qc()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _render_fixture(
                root / "final.mp4",
                width=1920,
                height=1080,
                duration=2.0,
            )
            _write_support_files(root, duration=2.0)

            report = qc.run_final_master_qc(root)

            self.assertEqual(report["status"], "pass")
            self.assertTrue(report["full_decode_ok"])
            self.assertEqual(report["blocking_findings"], [])
            self.assertEqual(report["ai_calls_added"], 0)
            self.assertFalse(report["final_media_mutated"])

    def test_wrong_dimensions_fail_closed_in_unchanged_legacy_core(self) -> None:
        qc = _load_unchanged_legacy_qc()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _render_fixture(
                root / "final.mp4",
                width=1280,
                height=720,
                duration=2.0,
            )
            _write_support_files(root, duration=2.0)

            with self.assertRaises(qc.FinalMasterQCError):
                qc.run_final_master_qc(root)

            report = json.loads(
                (root / "final-master-qc.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "block")
            self.assertTrue(
                any(
                    finding.startswith("unexpected_dimensions=")
                    for finding in report["blocking_findings"]
                )
            )


if __name__ == "__main__":
    unittest.main()
