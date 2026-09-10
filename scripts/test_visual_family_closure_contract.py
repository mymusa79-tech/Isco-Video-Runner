from __future__ import annotations

"""Zero-provider integration-contract tests for the visual family closure.

These tests run under ``unittest discover``. Final Master's own exact SHA/byte
validator is covered by its dedicated suite; here we patch only the Viewer import
seam to prove that Viewer requires a fresh acceptance receipt, requires the Short
timeline in that receipt, detects receipt drift, keeps Story on the Long path, and
never gives Run #228 authority over Long.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import viewer_quality_contract_v1 as viewer
from scripts.viewer_regression_run228 import enforce_run_228_viewer_regression


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _viewer_fixture(root: Path, *, fmt: str, derived: bool = False, short_timeline: bool = True) -> None:
    plan = {"format": fmt, "sections": [{"id": "s1"}]}
    if derived:
        plan["plan_source"] = "source_derived_long_episode_video_short"
    _write(root / "plan.json", plan)
    _write(
        root / "final-master-qc.json",
        {
            "status": "pass",
            "full_decode_ok": True,
            "blocking_findings": [],
            "detectors": {"exact_freeze": {"events": []}},
        },
    )
    _write(
        root / "quality-final.json",
        {
            "duration_ok": True,
            "audio_ok": True,
            "av_sync_ok": True,
            "video_streams": 1,
            "audio_streams": 1,
            "audio_measurement": {"integrated_lufs": -16.0},
            "audio_target_lufs": -16.0,
            "av_delta_seconds": 0.01,
        },
    )
    _write(
        root / "visual-audit.json",
        [
            {
                "status": "pass",
                "is_selected": True,
                "section": "s1",
                "provider": "pexels",
                "candidate_id": "42",
                "relevance": 0.90,
                "visual_quality": 0.91,
            }
        ],
    )
    if short_timeline:
        _write(
            root / "short-visual-timeline.json",
            {
                "duration_seconds": 9.0,
                "shot_count": 3,
                "semantic_beat_count": 3,
                "visual_density_contract": {"actual_max_shot_hold_seconds": 3.0},
            },
        )


def _p4(*, bind_short: bool = True, final_sha: str = "a" * 64, timeline_sha: str = "b" * 64) -> dict:
    sources = {
        "final": {"file": "final.mp4", "sha256": final_sha, "byte_length": 12345},
        "plan": {"file": "plan.json", "sha256": "c" * 64, "byte_length": 100},
        "quality_final": {"file": "quality-final.json", "sha256": "d" * 64, "byte_length": 100},
    }
    if bind_short:
        sources["short_visual_timeline"] = {
            "file": "short-visual-timeline.json",
            "sha256": timeline_sha,
            "byte_length": 200,
        }
    return {
        "acceptance_contract": {
            "contract_id": "final.master.acceptance.v2",
            "sources": sources,
        }
    }


def _critic() -> dict:
    return {"status": "pass", "hard_blocks": [], "observation_status": "ok"}


class VisualFamilyClosureContractTests(unittest.TestCase):
    def test_standalone_short_requires_timeline_in_final_master_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _viewer_fixture(root, fmt="moment")
            with patch.object(viewer, "require_final_master_acceptance", return_value=_p4(bind_short=False)):
                with self.assertRaisesRegex(RuntimeError, "short-visual-timeline.json to be sealed by Final Master"):
                    viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())

    def test_derived_short_uses_same_timeline_binding_and_short_regression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _viewer_fixture(root, fmt="moment", derived=True)
            with patch.object(viewer, "require_final_master_acceptance", return_value=_p4(bind_short=True)):
                result = viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())
            self.assertEqual(result["release_profile"], "derived_short")
            self.assertTrue(result["final_master_binding"]["short_visual_timeline_bound"])
            self.assertEqual(result["viewer_regression"]["status"], "pass")
            regression = json.loads((root / "viewer-regression-run-228.json").read_text(encoding="utf-8"))
            self.assertEqual(regression["release_profile"], "derived_short")
            self.assertEqual(regression["status"], "pass")

    def test_final_master_binding_drift_during_viewer_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _viewer_fixture(root, fmt="moment")
            receipts = iter(
                [
                    _p4(bind_short=True, final_sha="a" * 64),
                    _p4(bind_short=True, final_sha="e" * 64),
                ]
            )
            with patch.object(viewer, "require_final_master_acceptance", side_effect=lambda output_dir: next(receipts)):
                with self.assertRaisesRegex(RuntimeError, "Final Master binding drift"):
                    viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())

    def test_story_is_long_and_never_consumes_short_timeline_or_run228_floor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _viewer_fixture(root, fmt="story", short_timeline=False)
            with patch.object(viewer, "require_final_master_acceptance", return_value=_p4(bind_short=False)):
                result = viewer.enforce_viewer_quality_contract(root, fmt="story", critic=_critic())
            self.assertEqual(result["release_profile"], "story")
            self.assertEqual(result["dimensions"]["pacing"]["format_class"], "long")
            self.assertEqual(
                result["dimensions"]["pacing"]["long_specific_regression_calibration"],
                "not_yet_calibrated",
            )
            self.assertEqual(result["viewer_regression"]["status"], "not_applicable")
            self.assertFalse((root / "short-visual-timeline.json").exists())

    def test_run228_returns_before_baseline_read_for_long_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for profile in ("film", "story"):
                with self.subTest(profile=profile):
                    result = enforce_run_228_viewer_regression(
                        root,
                        viewer_report={"release_profile": profile},
                        baseline_path=root / "does-not-exist.json",
                    )
                    self.assertEqual(result["status"], "not_applicable")
                    self.assertEqual(result["release_profile"], profile)


if __name__ == "__main__":
    unittest.main()
