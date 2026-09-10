from __future__ import annotations

"""Zero-provider contract tests for the visual family closure.

Run directly from repository root:
    python scripts/test_visual_family_closure_contract.py

The tests intentionally stub Final Master verification only at the Viewer import seam;
Final Master's own exact SHA/byte validation remains covered by its existing contract.
Here we verify that Viewer *requires* that fresh receipt, binds Short timeline presence,
keeps Story on the Long path, and applies Run #228 only to Shorts.
"""

import json
import sys
import tempfile
from pathlib import Path
from types import ModuleType


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_stub_final_master = ModuleType("scripts.final_master_acceptance_v2")
_stub_final_master.require_final_master_acceptance = lambda output_dir: {}
sys.modules["scripts.final_master_acceptance_v2"] = _stub_final_master

from scripts import viewer_quality_contract_v1 as viewer  # noqa: E402
from scripts.viewer_regression_run228 import enforce_run_228_viewer_regression  # noqa: E402


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


def test_standalone_short_requires_timeline_in_final_master_receipt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _viewer_fixture(root, fmt="moment")
        viewer.require_final_master_acceptance = lambda output_dir: _p4(bind_short=False)
        try:
            viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())
        except RuntimeError as exc:
            assert "short-visual-timeline.json to be sealed by Final Master" in str(exc)
        else:
            raise AssertionError("Standalone Short accepted without timeline binding")


def test_derived_short_uses_same_timeline_binding_and_run228_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _viewer_fixture(root, fmt="moment", derived=True)
        viewer.require_final_master_acceptance = lambda output_dir: _p4(bind_short=True)
        result = viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())
        assert result["release_profile"] == "derived_short"
        assert result["final_master_binding"]["short_visual_timeline_bound"] is True
        assert result["viewer_regression"]["status"] == "pass"
        regression = json.loads((root / "viewer-regression-run-228.json").read_text(encoding="utf-8"))
        assert regression["release_profile"] == "derived_short"
        assert regression["status"] == "pass"


def test_final_master_binding_drift_during_viewer_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _viewer_fixture(root, fmt="moment")
        receipts = iter([
            _p4(bind_short=True, final_sha="a" * 64),
            _p4(bind_short=True, final_sha="e" * 64),
        ])
        viewer.require_final_master_acceptance = lambda output_dir: next(receipts)
        try:
            viewer.enforce_viewer_quality_contract(root, fmt="moment", critic=_critic())
        except RuntimeError as exc:
            assert "Final Master binding drift" in str(exc)
        else:
            raise AssertionError("Viewer accepted drifted Final Master binding")


def test_story_is_long_and_never_consumes_short_timeline_or_run228_floor() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _viewer_fixture(root, fmt="story", short_timeline=False)
        viewer.require_final_master_acceptance = lambda output_dir: _p4(bind_short=False)
        result = viewer.enforce_viewer_quality_contract(root, fmt="story", critic=_critic())
        assert result["release_profile"] == "story"
        assert result["dimensions"]["pacing"]["format_class"] == "long"
        assert result["dimensions"]["pacing"]["long_specific_regression_calibration"] == "not_yet_calibrated"
        assert result["viewer_regression"]["status"] == "not_applicable"
        assert not (root / "short-visual-timeline.json").exists()


def test_run228_returns_before_baseline_read_for_long_profiles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for profile in ("film", "story"):
            report = {"release_profile": profile}
            result = enforce_run_228_viewer_regression(
                root,
                viewer_report=report,
                baseline_path=root / "does-not-exist.json",
            )
            assert result["status"] == "not_applicable"
            assert result["release_profile"] == profile


def main() -> None:
    tests = [
        test_standalone_short_requires_timeline_in_final_master_receipt,
        test_derived_short_uses_same_timeline_binding_and_run228_gate,
        test_final_master_binding_drift_during_viewer_fails_closed,
        test_story_is_long_and_never_consumes_short_timeline_or_run228_floor,
        test_run228_returns_before_baseline_read_for_long_profiles,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS visual family closure contract: {len(tests)} tests, provider_calls=0")


if __name__ == "__main__":
    main()
