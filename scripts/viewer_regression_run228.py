from __future__ import annotations

"""Deterministic Short-only regression guard anchored to Run #228.

Run #228 is a human-reviewed Moment reference. Its observations can calibrate future
Short releases, including derived Shorts, but must never govern Long releases. The
historical 6.5-7.0/10 human score is retained as non-enforcing context and is not
converted into the engineering Viewer Quality score.

No provider/model calls are made here. The guard consumes the already-produced
Viewer Quality document and writes an auditable receipt before state acceptance.
"""

import json
from pathlib import Path
from typing import Any


SCHEMA = "isco.viewer-regression-run-228.v1"
BASELINE_SCHEMA = "isco.viewer-regression-baseline.v1"
BASELINE_RUN = 228
REPORT_NAME = "viewer-regression-run-228.json"
BASELINE_PATH = Path(__file__).resolve().parents[1] / ".ops" / "viewer-baseline-run-228.json"
SHORT_PROFILES = {"standalone_short", "derived_short"}


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Run #228 viewer regression requires valid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Run #228 viewer regression requires JSON object for {label}")
    return value


def _number(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Run #228 viewer regression requires numeric {label}") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise RuntimeError(f"Run #228 viewer regression requires finite {label}")
    return result


def _write_report(root: Path, payload: dict[str, Any]) -> None:
    (root / REPORT_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def enforce_run_228_viewer_regression(
    output_dir: str | Path,
    *,
    viewer_report: dict[str, Any] | None = None,
    baseline_path: str | Path | None = None,
) -> dict[str, Any]:
    """Fail closed on Short regression; explicitly do not apply #228 to Long."""
    root = Path(output_dir)
    report = (
        dict(viewer_report)
        if isinstance(viewer_report, dict)
        else _read_object(root / "viewer-quality-contract.json", label="Viewer Quality report")
    )
    profile = str(report.get("release_profile") or "").strip().lower()
    if profile not in SHORT_PROFILES:
        result = {
            "schema": SCHEMA,
            "baseline_run": BASELINE_RUN,
            "release_profile": profile,
            "status": "not_applicable",
            "reason": "Run #228 is a Short/Moment reference and is not calibrated for Long releases",
            "long_regression_policy": "requires independent long-specific benchmark set and range calibration",
            "provider_calls_added": 0,
        }
        _write_report(root, result)
        return result

    baseline_file = Path(baseline_path) if baseline_path is not None else BASELINE_PATH
    baseline = _read_object(baseline_file, label="machine baseline")
    if baseline.get("schema") != BASELINE_SCHEMA or int(baseline.get("baseline_run") or 0) != BASELINE_RUN:
        raise RuntimeError("Run #228 viewer regression baseline identity mismatch")
    applicable = {str(item).strip().lower() for item in baseline.get("applicable_release_profiles") or ()}
    if profile not in applicable or applicable != SHORT_PROFILES:
        raise RuntimeError("Run #228 viewer regression baseline scope mismatch")

    human = baseline.get("human_observation")
    if not isinstance(human, dict) or human.get("enforced") is not False:
        raise RuntimeError("Run #228 human viewer observation must remain non-enforcing")

    long_policy = baseline.get("long_policy")
    if not isinstance(long_policy, dict) or long_policy.get("run_228_governs_long") is not False:
        raise RuntimeError("Run #228 baseline must explicitly forbid Long governance")

    floor = baseline.get("machine_regression_floor")
    if not isinstance(floor, dict):
        raise RuntimeError("Run #228 viewer regression baseline lacks machine floor")

    dimensions = report.get("dimensions")
    if not isinstance(dimensions, dict):
        raise RuntimeError("Run #228 viewer regression requires Viewer Quality dimensions")
    visual = dimensions.get("visual_semantics")
    pacing = dimensions.get("pacing")
    technical = dimensions.get("technical")
    gold = dimensions.get("gold")
    if not all(isinstance(item, dict) for item in (visual, pacing, technical, gold)):
        raise RuntimeError("Run #228 viewer regression requires visual/pacing/technical/gold evidence")

    observed = {
        "viewer_score_10": _number(report.get("viewer_score_10"), label="viewer score"),
        "visual_semantics_score_10": _number(visual.get("score_10"), label="visual semantic score"),
        "short_pacing_score_10": _number(pacing.get("score_10"), label="short pacing score"),
        "weakest_final_visual_semantic_floor": _number(
            visual.get("semantic_min"), label="weakest final visual semantic floor"
        ),
        "gold_pass": gold.get("pass") is True,
        "technical_pass": technical.get("pass") is True,
        "viewer_verdict_pass": str(report.get("verdict") or "").lower() == "pass",
    }

    required = {
        "viewer_score_10": _number(floor.get("viewer_score_10"), label="baseline viewer floor"),
        "visual_semantics_score_10": _number(
            floor.get("visual_semantics_score_10"), label="baseline visual floor"
        ),
        "short_pacing_score_10": _number(
            floor.get("short_pacing_score_10"), label="baseline pacing floor"
        ),
        "weakest_final_visual_semantic_floor": _number(
            floor.get("weakest_final_visual_semantic_floor"), label="baseline weakest semantic floor"
        ),
        "gold_must_pass": floor.get("gold_must_pass") is True,
        "technical_must_pass": floor.get("technical_must_pass") is True,
    }

    checks = {
        "viewer_contract_passed": observed["viewer_verdict_pass"],
        "viewer_score_not_regressed": observed["viewer_score_10"] >= required["viewer_score_10"],
        "visual_semantics_not_regressed": (
            observed["visual_semantics_score_10"] >= required["visual_semantics_score_10"]
        ),
        "weakest_final_visual_not_regressed": (
            observed["weakest_final_visual_semantic_floor"]
            >= required["weakest_final_visual_semantic_floor"]
        ),
        "short_pacing_not_regressed": observed["short_pacing_score_10"] >= required["short_pacing_score_10"],
        "gold_continuity_gate_passed": (
            (not required["gold_must_pass"]) or observed["gold_pass"]
        ),
        "technical_gate_passed": (
            (not required["technical_must_pass"]) or observed["technical_pass"]
        ),
    }
    failures = sorted(name for name, ok in checks.items() if not ok)
    result = {
        "schema": SCHEMA,
        "baseline_schema": BASELINE_SCHEMA,
        "baseline_run": BASELINE_RUN,
        "release_profile": profile,
        "status": "pass" if not failures else "block",
        "observed": observed,
        "required_machine_floor": required,
        "checks": checks,
        "failures": failures,
        "human_baseline_score_enforced": False,
        "run_228_governs_long": False,
        "score_semantics": "Short machine regression floor only; not a conversion of the historical human 6.5-7.0 score",
        "provider_calls_added": 0,
    }
    _write_report(root, result)
    if failures:
        raise RuntimeError(
            "Run #228 Viewer Regression blocked Short release: " + ",".join(failures)
        )
    return result
