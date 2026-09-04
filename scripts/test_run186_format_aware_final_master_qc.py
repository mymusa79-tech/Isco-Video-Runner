from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import final_master_qc as qc
from scripts import orchestration_shorts_port as short_port


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _probe(*, seconds: float = 12.0, fmt: str = "moment") -> dict:
    width, height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    return {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
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
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
        "format": {"duration": str(seconds)},
    }


def _scan() -> dict:
    return {
        "returncode": 0,
        "timed_out": False,
        "black_events": [],
        "silence_events": [],
        "freeze_events": [],
        "stderr": "",
    }


def _moment_root(td: str, *, duration: float = 12.0) -> Path:
    root = Path(td)
    (root / "final.mp4").write_bytes(b"final" * 1024)
    _write(root / "plan.json", {"format": "moment"})
    _write(
        root / "quality-final.json",
        {
            "format": "moment",
            "duration_seconds": duration,
            "video_stream_duration": duration,
            "audio_stream_duration": duration,
            "quality_measurement_stage": "post_render",
        },
    )
    return root


class Run186FormatAwareFinalMasterQCTests(unittest.TestCase):
    def test_observed_short_base_master_passes_without_m7_visual_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td)
            self.assertFalse((root / "visual-timeline.json").exists())
            with patch.object(qc, "probe", return_value=_probe()), patch.object(
                qc, "_run_full_scan", return_value=_scan()
            ):
                report = qc.run_final_master_qc(root)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["body_contract_kind"], "moment_measured_render")
            self.assertEqual(report["body_duration_source"], "quality-final.json:video_stream_duration")
            self.assertFalse(report["m7_timeline_authoritative"])
            self.assertFalse(report["short_timeline_authoritative"])
            self.assertEqual(report["body_duration_seconds"], 12.0)
            self.assertEqual(report["m7_body_duration_seconds"], 12.0)

    def test_long_form_still_fails_closed_without_m7_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"final" * 1024)
            _write(root / "plan.json", {"format": "film"})
            _write(root / "quality-final.json", {"format": "film", "video_stream_duration": 60.0})
            with self.assertRaisesRegex(qc.FinalMasterQCError, "visual-timeline.json"):
                qc.run_final_master_qc(root)

    def test_long_form_keeps_m7_timeline_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"final" * 1024)
            _write(root / "plan.json", {"format": "film"})
            _write(root / "quality-final.json", {"format": "film", "video_stream_duration": 60.0})
            _write(root / "visual-timeline.json", {"duration_seconds": 60.0})
            with patch.object(qc, "probe", return_value=_probe(seconds=60.0, fmt="film")), patch.object(
                qc, "_run_full_scan", return_value=_scan()
            ):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["body_contract_kind"], "long_m7_timeline")
            self.assertEqual(report["body_duration_source"], "visual-timeline.json:duration_seconds")
            self.assertTrue(report["m7_timeline_authoritative"])

    def test_finished_standalone_short_prefers_real_short_cinematic_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td)
            _write(
                root / "short-visual-timeline.json",
                {
                    "schema_version": 1,
                    "profile": "short_cinematic_director_v1",
                    "status": "applied",
                    "duration_seconds": 12.0,
                    "shot_count": 3,
                },
            )
            with patch.object(qc, "probe", return_value=_probe()), patch.object(
                qc, "_run_full_scan", return_value=_scan()
            ):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["body_contract_kind"], "moment_short_cinematic_timeline")
            self.assertEqual(report["body_duration_source"], "short-visual-timeline.json:duration_seconds")
            self.assertTrue(report["short_timeline_authoritative"])
            self.assertEqual(report["quality_duration_crosscheck_seconds"], 12.0)

    def test_sibling_short_without_cinematic_timeline_uses_refreshed_quality_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td, duration=14.0)
            _write(
                root / "quality-final.json",
                {
                    "format": "moment",
                    "video_stream_duration": 14.0,
                    "duration_seconds": 14.0,
                    "quality_measurement_stage": "post_short_finishing_pre_gold",
                },
            )
            with patch.object(qc, "probe", return_value=_probe(seconds=14.0)), patch.object(
                qc, "_run_full_scan", return_value=_scan()
            ):
                report = qc.run_final_master_qc(root)
            self.assertEqual(report["body_contract_kind"], "moment_measured_render")
            self.assertEqual(report["body_duration_source"], "quality-final.json:video_stream_duration")
            self.assertFalse(report["short_timeline_authoritative"])

    def test_existing_malformed_short_timeline_blocks_instead_of_silent_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td)
            (root / "short-visual-timeline.json").write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(qc.FinalMasterQCError, "short-visual-timeline.json"):
                qc.run_final_master_qc(root)

    def test_short_timeline_and_quality_duration_disagreement_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td, duration=12.0)
            _write(root / "short-visual-timeline.json", {"duration_seconds": 10.0})
            with self.assertRaisesRegex(qc.FinalMasterQCError, "body-duration authority mismatch"):
                qc.run_final_master_qc(root)

    def test_moment_probe_and_body_duration_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td, duration=12.0)
            with patch.object(qc, "probe", return_value=_probe(seconds=12.5)):
                with self.assertRaisesRegex(qc.FinalMasterQCError, "Moment final/body duration mismatch"):
                    qc.run_final_master_qc(root)

    def test_short_double_qc_handoff_crosses_base_qc_then_rechecks_finished_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _moment_root(td)
            request = {"kind": "short", "request_id": "run186-short"}
            with patch.object(qc, "probe", return_value=_probe()), patch.object(
                qc, "_run_full_scan", return_value=_scan()
            ):
                base_report = qc.run_final_master_qc(root)
                self.assertEqual(base_report["body_contract_kind"], "moment_measured_render")

                def voice(_out, _request, pre_gold, *, ledger):
                    _write(
                        root / "short-visual-timeline.json",
                        {"status": "applied", "duration_seconds": 12.0, "shot_count": 3},
                    )
                    _write(
                        root / "quality-final.json",
                        {
                            "format": "moment",
                            "video_stream_duration": 12.0,
                            "duration_seconds": 12.0,
                            "quality_measurement_stage": "post_short_finishing_pre_gold",
                        },
                    )
                    return {**pre_gold, "voice": {"generated": True}}

                with patch.object(
                    short_port.core, "prepare_short_render", return_value={"stage": "pre_gold"}
                ), patch.object(short_port, "apply_short_voice_v2", side_effect=voice):
                    result = short_port.prepare_authoritative_short_for_gold(
                        root,
                        request,
                        ledger=object(),
                        run_final_master_qc=qc.run_final_master_qc,
                    )

            self.assertTrue(result["authoritative_final_master_qc_rerun"])
            finished_report = json.loads((root / "final-master-qc.json").read_text(encoding="utf-8"))
            self.assertEqual(finished_report["status"], "pass")
            self.assertEqual(
                finished_report["body_contract_kind"],
                "moment_short_cinematic_timeline",
            )


if __name__ == "__main__":
    unittest.main()
