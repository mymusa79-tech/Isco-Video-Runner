from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import final_master_acceptance_v2 as acceptance
from scripts import final_master_acceptance_v2_legacy as acceptance_legacy
from scripts import final_master_format_router as router
from scripts import final_master_qc as core
from scripts.final_master_body_contract import (
    FinalMasterBodyContractError,
    resolve_body_contract,
)


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _probe(*, seconds: float, fmt: str = "moment") -> dict:
    width, height = ((1080, 1920) if fmt == "moment" else (1920, 1080))
    return {
        "format": {"duration": str(seconds)},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "profile": "High",
                "width": width,
                "height": height,
                "pix_fmt": "yuv420p",
                "avg_frame_rate": "30/1",
                "field_order": "progressive",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
                "color_space": "bt709",
            }
        ],
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


class Run186FormatAwareFinalMasterQCTests(unittest.TestCase):
    def _moment_root(self, td: str, *, seconds: float = 12.0) -> Path:
        root = Path(td)
        (root / "final.mp4").write_bytes(b"moment-final-bytes")
        _write(root / "plan.json", {"format": "moment"})
        _write(
            root / "quality-final.json",
            {
                "format": "moment",
                "duration_ok": True,
                "audio_ok": True,
                "av_sync_ok": True,
                "video_stream_duration": seconds,
                "duration_seconds": seconds,
            },
        )
        return root

    def test_observed_production_shape_passes_without_long_m7_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            self.assertFalse((root / "visual-timeline.json").exists())
            original = Mock(side_effect=AssertionError("Moment must not enter long-only core body lookup"))
            with patch.object(router.core, "probe", return_value=_probe(seconds=12.0)), patch.object(
                router.core, "_run_full_scan", return_value=_scan()
            ):
                report = router.run_format_aware_final_master_qc(root, original=original)

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["format"], "moment")
            self.assertEqual(report["body_contract_kind"], "moment_measured_render")
            self.assertEqual(report["body_duration_source"], "quality-final.json:video_stream_duration")
            self.assertFalse(report["m7_timeline_authoritative"])
            original.assert_not_called()

    def test_long_form_delegates_to_certified_core_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "plan.json", {"format": "film"})
            _write(root / "quality-final.json", {"format": "film"})
            expected = {"status": "pass", "owner": "certified-core"}
            original = Mock(return_value=expected)
            result = router.run_format_aware_final_master_qc(root, original=original)
            self.assertIs(result, expected)
            original.assert_called_once_with(root)

    def test_standalone_short_timeline_is_authoritative_only_after_it_exists(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            base = resolve_body_contract(
                root,
                fmt="moment",
                quality=json.loads((root / "quality-final.json").read_text()),
            )
            self.assertEqual(base["kind"], "moment_measured_render")

            _write(root / "short-visual-timeline.json", {"duration_seconds": 12.0})
            finished = resolve_body_contract(
                root,
                fmt="moment",
                quality=json.loads((root / "quality-final.json").read_text()),
            )
            self.assertEqual(finished["kind"], "moment_short_cinematic_timeline")
            self.assertTrue(finished["short_timeline_authoritative"])

    def test_malformed_existing_short_timeline_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            (root / "short-visual-timeline.json").write_text("{bad", encoding="utf-8")
            with self.assertRaises(FinalMasterBodyContractError):
                resolve_body_contract(
                    root,
                    fmt="moment",
                    quality=json.loads((root / "quality-final.json").read_text()),
                )

    def test_conflicting_short_and_measured_duration_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            _write(root / "short-visual-timeline.json", {"duration_seconds": 10.0})
            with self.assertRaises(FinalMasterBodyContractError):
                resolve_body_contract(
                    root,
                    fmt="moment",
                    quality=json.loads((root / "quality-final.json").read_text()),
                )

    def test_moment_probe_and_body_duration_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            with patch.object(router.core, "probe", return_value=_probe(seconds=13.0)), patch.object(
                router.core, "_run_full_scan", return_value=_scan()
            ):
                with self.assertRaises(core.FinalMasterQCError):
                    router.run_format_aware_final_master_qc(root, original=Mock())

    def test_moment_acceptance_receipt_owns_no_long_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            with patch.object(router.core, "probe", return_value=_probe(seconds=12.0)), patch.object(
                router.core, "_run_full_scan", return_value=_scan()
            ):
                report = router.run_format_aware_final_master_qc(root, original=Mock())

            with patch.object(acceptance_legacy, "probe", return_value=_probe(seconds=12.0)):
                sealed = acceptance.seal_final_master_acceptance(root, report)
                required = acceptance.require_final_master_acceptance(root, report=sealed)

            sources = required["acceptance_contract"]["sources"]
            self.assertEqual(set(sources), {"final", "plan", "quality_final"})
            self.assertNotIn("visual_timeline", sources)

    def test_second_qc_after_short_finishing_binds_short_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = self._moment_root(td)
            with patch.object(router.core, "probe", return_value=_probe(seconds=12.0)), patch.object(
                router.core, "_run_full_scan", return_value=_scan()
            ), patch.object(acceptance_legacy, "probe", return_value=_probe(seconds=12.0)):
                first = router.run_format_aware_final_master_qc(root, original=Mock())
                first = acceptance.seal_final_master_acceptance(root, first)
                self.assertNotIn(
                    "short_visual_timeline",
                    first["acceptance_contract"]["sources"],
                )

                _write(root / "short-visual-timeline.json", {"duration_seconds": 12.0})
                second = router.run_format_aware_final_master_qc(root, original=Mock())
                second = acceptance.seal_final_master_acceptance(root, second)

            self.assertEqual(
                second["body_contract_kind"],
                "moment_short_cinematic_timeline",
            )
            self.assertIn(
                "short_visual_timeline",
                second["acceptance_contract"]["sources"],
            )
            acceptance.require_final_master_acceptance(root, report=second)

    def test_long_acceptance_still_requires_m7_visual_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "final.mp4").write_bytes(b"long-final")
            _write(root / "plan.json", {"format": "film"})
            _write(root / "quality-final.json", {"format": "film"})
            with self.assertRaises(acceptance.FinalMasterAcceptanceError):
                acceptance._source_bindings(root)

    def test_certified_core_and_stable_port_remain_original_blobs(self) -> None:
        def blob(path: str) -> str:
            data = Path(path).read_bytes()
            return hashlib.sha1(
                f"blob {len(data)}\0".encode("ascii") + data
            ).hexdigest()

        self.assertEqual(
            blob("scripts/final_master_qc.py"),
            "e3412fc5710618eb9d7529710d8dbbc539e9fa91",
        )
        self.assertEqual(
            blob("scripts/orchestration_qc_port.py"),
            "9d23051dc3db8ad8f5913dd5a21dcc2f4bee7035",
        )

    def test_router_reuses_certified_core_thresholds_and_scanners(self) -> None:
        source = Path("scripts/final_master_format_router.py").read_text(encoding="utf-8")
        for required in (
            "core._stream_contract",
            "core._run_full_scan",
            "core._parse_silence_events",
            "core._parse_freeze_events",
            "core.BLACK_DETECT_SECONDS",
            "core.SILENCE_DETECT_SECONDS",
            "core.FREEZE_BLOCK_SECONDS",
            "core.FULL_SCAN_TIMEOUT_SECONDS",
        ):
            self.assertIn(required, source)
        for forbidden in (
            "BLACK_DETECT_SECONDS =",
            "SILENCE_DETECT_SECONDS =",
            "FREEZE_BLOCK_SECONDS =",
            "FULL_SCAN_TIMEOUT_SECONDS =",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
