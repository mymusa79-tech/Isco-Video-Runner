from __future__ import annotations

"""Deterministic Short regression guard with Run #228 as a human reference only.

Run #228 is a human-reviewed Moment reference. It contributes known viewer failure
signatures, but it does not supply numeric machine thresholds and never governs Long.
The blocking Short floors in this module come from the existing Viewer Quality release
envelope, preserved in a separate profile contract so future code changes cannot
silently weaken them.

Some #228 observations (notably hook-to-body continuity and payoff specificity) do not
yet have honest deterministic signals in the current pipeline. They are recorded as
not-yet-machine-calibrated rather than replaced by invented proxy scores.

No provider/model calls are made here. The guard consumes already-produced Viewer
Quality evidence and writes an auditable receipt before state acceptance.
"""

import json
from pathlib import Path
from typing import Any


SCHEMA = "isco.viewer-regression-run-228.v1"
BASELINE_SCHEMA = "isco.viewer-regression-baseline.v1"
CONTRACT_ID = "viewer-regression-profile.v1"
BASELINE_RUN = 228
REPORT_NAME = "viewer-regression-run-228.json"
BASELINE_PATH = Path(__file__).resolve().parents[1] / ".ops" / "viewer-baseline-run-228.json"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / ".ops" / "viewer-regression-contract-v1.json"
SHORT_PROFILES = {"standalone_short", "derived_short"}
LONG_PROFILES = {"film", "story"}


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Short Viewer Regression requires valid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Short Viewer Regression requires JSON object for {label}")
    return value


def _number(value: Any, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Short Viewer Regression requires numeric {label}") from exc
    if result != result or result in {float("inf"), float("-inf")}:
        raise RuntimeError(f"Short Viewer Regression requires finite {label}")
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
    contract_path: str | Path | None = None,
) -> dict[str, Any]:
    """Protect Short release invariants; explicitly give #228 no Long authority."""
    root = Path(output_dir)
    report = (
        dict(viewer_report)
        if isinstance(viewer_report, dict)
        else _read_object(root / "viewer-quality-contract.json", label="Viewer Quality report")
    )
    profile = str(report.get("release_profile") or "").strip().lower()

    # Critical boundary: Long returns before either the #228 reference or Short machine
    # contract is loaded. A missing/corrupt #228 file therefore cannot affect Film/Story.
    if profile not in SHORT_PROFILES:
        result = {
            "schema": SCHEMA,
            "reference_run": BASELINE_RUN,
            "reference_role": "qualitative_short_reference_only",
            "release_profile": profile,
            "status": "not_applicable",
            "reason": "Run #228 is a Short/Moment human reference and has no Long release authority",
            "long_regression_policy": "requires independent long-specific multi-video benchmark set and range calibration",
            "provider_calls_added": 0,
        }
        _write_report(root, result)
        return result

    baseline_file = Path(baseline_path) if baseline_path is not None else BASELINE_PATH
    reference = _read_object(baseline_file, label="Run #228 human reference")
    if reference.get("schema") != BASELINE_SCHEMA or int(reference.get("baseline_run") or 0) != BASELINE_RUN:
        raise RuntimeError("Run #228 viewer reference identity mismatch")
    if reference.get("role") != "qualitative_short_reference_only":
        raise RuntimeError("Run #228 viewer reference must remain qualitative-only")
    applicable = {str(item).strip().lower() for item in reference.get("applicable_release_profiles") or ()}
    if profile not in applicable or applicable != SHORT_PROFILES:
        raise RuntimeError("Run #228 viewer reference scope mismatch")

    human = reference.get("human_observation")
    if (
        not isinstance(human, dict)
        or human.get("enforced") is not False
        or human.get("machine_threshold_source") is not False
    ):
        raise RuntimeError("Run #228 human viewer observation must remain non-enforcing")
    long_policy = reference.get("long_policy")
    if not isinstance(long_policy, dict) or long_policy.get("run_228_governs_long") is not False:
        raise RuntimeError("Run #228 viewer reference must explicitly forbid Long governance")

    contract_file = Path(contract_path) if contract_path is not None else CONTRACT_PATH
    contract = _read_object(contract_file, label="Short machine regression contract")
    if int(contract.get("schema_version") or 0) != 1 or contract.get("contract_id") != CONTRACT_ID:
        raise RuntimeError("Short Viewer Regression contract identity mismatch")
    principles = contract.get("principles")
    if (
        not isinstance(principles, dict)
        or principles.get("human_score_is_machine_threshold") is not False
        or principles.get("run_228_is_machine_threshold_source") is not False
    ):
        raise RuntimeError("Short Viewer Regression contract must forbid human/#228 numeric threshold authority")

    profile_contract = (contract.get("profiles") or {}).get(profile)
    if not isinstance(profile_contract, dict) or profile_contract.get("machine_gate_operational") is not True:
        raise RuntimeError(f"Short Viewer Regression lacks operational profile contract: {profile}")
    references = profile_contract.get("human_references") or ()
    run228_refs = [
        item
        for item in references
        if isinstance(item, dict) and int(item.get("run") or 0) == BASELINE_RUN
    ]
    if len(run228_refs) != 1 or run228_refs[0].get("authority") != "qualitative_reference_only":
        raise RuntimeError("Short Viewer Regression profile must keep Run #228 qualitative-only")

    floor = contract.get("short_machine_release_invariants")
    if not isinstance(floor, dict):
        raise RuntimeError("Short Viewer Regression contract lacks machine release invariants")
    if floor.get("source") != "existing_viewer_quality_absolute_release_envelope":
        raise RuntimeError("Short Viewer Regression machine floor source mismatch")
    if floor.get("calibration_source") != "not_run_228":
        raise RuntimeError("Short Viewer Regression must not claim Run #228 numeric calibration")

    dimensions = report.get("dimensions")
    if not isinstance(dimensions, dict):
        raise RuntimeError("Short Viewer Regression requires Viewer Quality dimensions")
    visual = dimensions.get("visual_semantics")
    pacing = dimensions.get("pacing")
    technical = dimensions.get("technical")
    gold = dimensions.get("gold")
    if not all(isinstance(item, dict) for item in (visual, pacing, technical, gold)):
        raise RuntimeError("Short Viewer Regression requires visual/pacing/technical/gold evidence")
    if str(pacing.get("format_class") or "").lower() != "short":
        raise RuntimeError("Short Viewer Regression requires Short pacing evidence")

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
        "viewer_score_10": _number(floor.get("viewer_score_10"), label="Short viewer absolute floor"),
        "visual_semantics_score_10": _number(
            floor.get("visual_semantics_score_10"), label="Short visual absolute floor"
        ),
        "short_pacing_score_10": _number(
            floor.get("short_pacing_score_10"), label="Short pacing absolute floor"
        ),
        "weakest_final_visual_semantic_floor": _number(
            floor.get("weakest_final_visual_semantic_floor"), label="Short weakest semantic floor"
        ),
        "gold_must_pass": floor.get("gold_must_pass") is True,
        "technical_must_pass": floor.get("technical_must_pass") is True,
    }

    checks = {
        "viewer_contract_passed": observed["viewer_verdict_pass"],
        "viewer_absolute_floor_preserved": observed["viewer_score_10"] >= required["viewer_score_10"],
        "visual_semantics_floor_preserved": (
            observed["visual_semantics_score_10"] >= required["visual_semantics_score_10"]
        ),
        "weakest_final_visual_floor_preserved": (
            observed["weakest_final_visual_semantic_floor"]
            >= required["weakest_final_visual_semantic_floor"]
        ),
        "short_pacing_floor_preserved": observed["short_pacing_score_10"] >= required["short_pacing_score_10"],
        "gold_continuity_gate_passed": (
            (not required["gold_must_pass"]) or observed["gold_pass"]
        ),
        "technical_gate_passed": (
            (not required["technical_must_pass"]) or observed["technical_pass"]
        ),
    }
    failures = sorted(name for name, ok in checks.items() if not ok)

    signature_coverage = profile_contract.get("known_failure_signature_coverage")
    if not isinstance(signature_coverage, dict):
        raise RuntimeError("Short Viewer Regression requires explicit human failure-signature coverage")
    required_signatures = {
        "hook_to_body_quality_cliff",
        "generic_mood_visual_instead_of_exact_meaning",
        "payoff_too_general_for_practical_topic",
    }
    if set(signature_coverage) != required_signatures:
        raise RuntimeError("Short Viewer Regression failure-signature coverage is incomplete")

    result = {
        "schema": SCHEMA,
        "baseline_schema": BASELINE_SCHEMA,
        "reference_run": BASELINE_RUN,
        "reference_role": "qualitative_short_reference_only",
        "release_profile": profile,
        "status": "pass" if not failures else "block",
        "machine_gate_calibration": "absolute_release_invariants_not_human_baseline",
        "human_failure_signature_calibration": profile_contract.get("human_failure_signature_calibration"),
        "known_failure_signature_coverage": signature_coverage,
        "observed": observed,
        "required_machine_release_invariants": required,
        "machine_floor_source": floor.get("source"),
        "machine_floor_calibration_source": floor.get("calibration_source"),
        "checks": checks,
        "failures": failures,
        "human_baseline_score_enforced": False,
        "run_228_supplies_numeric_thresholds": False,
        "run_228_governs_long": False,
        "score_semantics": "Short absolute Viewer release invariants; Run #228 contributes qualitative failure signatures only",
        "provider_calls_added": 0,
    }
    _write_report(root, result)
    if failures:
        raise RuntimeError(
            "Short Viewer Regression blocked release: " + ",".join(failures)
        )
    return result
