from __future__ import annotations

"""P5 adapter for the historical video-50 delivery-integrity replay.

video-50 is explicitly registered as an immutable delivery-integrity fixture and not as
a current visual-quality baseline. P5 already records provider-free boundaries for the
Gold critic, thumbnails, and rights. This adapter does the same for the newly inserted
Viewer Quality boundary while still executing the real ``run_p5`` Gold transaction,
same-render checks, packaging seal, and state-acceptance ordering.

This module is CI-only. Canonical production never imports it and the real Viewer Quality
contract remains fail-closed on missing evidence.
"""

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import scripts.gold_enforce_phase4 as gold
import scripts.production_stage_ladder as ladder


RECORDED_CONTRACT_ID = "viewer-quality.v1"
RECORDED_SCORE = 9.0


def _recorded_viewer_quality(output_dir: Path, *, fmt: str, critic: dict, **_kwargs) -> dict:
    root = Path(output_dir)
    if str(fmt).strip().lower() != "film":
        raise RuntimeError("P5 recorded Viewer Quality boundary is film-only")
    if str((critic or {}).get("status") or "").strip().lower() != "pass":
        raise RuntimeError("P5 recorded Viewer Quality boundary requires recorded Gold critic PASS")
    report = {
        "schema_version": 1,
        "contract_id": RECORDED_CONTRACT_ID,
        "format": "film",
        "release_profile": "film",
        "viewer_score_10": RECORDED_SCORE,
        "minimum_viewer_score_10": 8.5,
        "verdict": "pass",
        "acceptance_phase": "pre_state_acceptance",
        "score_meaning": "recorded_stage_ladder_boundary_not_visual_quality_measurement",
        "calibration_status": "not_applicable_historical_delivery_integrity_fixture",
        "stage_ladder_recorded_boundary": True,
        "baseline_role": ladder.BASELINE_ROLE,
        "provider_calls_added": 0,
    }
    (root / "viewer-quality-contract.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["phase"])
    parser.add_argument("phase", choices=["P5"])
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--staging", required=True, type=Path)
    args = parser.parse_args()
    with patch.object(
        gold,
        "enforce_viewer_quality_contract",
        side_effect=_recorded_viewer_quality,
    ):
        ladder.run_p5(args.evidence_dir.resolve(), args.staging.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
