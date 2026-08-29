from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
ENGINE_ROOT = ROOT / "engine"
REGISTER_PATH = Path(__file__).with_name("production_family_closure.json")
PHASES = ("P0", "P1", "P2", "P3", "P4", "P5", "P6")
BASELINE_TAG = "video-50"
BASELINE_ASSET = "final.mp4"
BASELINE_SIZE = 239_012_306
BASELINE_SHA256 = "143abdfb7b55f2269cab2c4634b68e7a99a5581ac08eceeccfb8cdda2ffdc742"


PHASE_TESTS: dict[str, tuple[str, ...]] = {
    "P0": (
        "scripts.test_environment_preflight",
        "scripts.test_persistent_memory",
        "scripts.test_persistent_memory_migration_epoch",
        "scripts.test_run129_hermetic_approved_brief",
        "scripts.test_run129_production_test_isolation_contract",
        "scripts.test_run130_explicit_runtime_phase",
        "scripts.test_run130_runtime_authority_contract",
        "scripts.test_runtime_closure",
        "scripts.test_state_persistence_strict",
    ),
    "P1": (
        "scripts.test_planner_quality_guard",
        "scripts.test_planner_schema_guard",
        "scripts.test_planning_provider_reliability_v2",
        "scripts.test_bounded_output_recovery",
        "scripts.test_schema_repair_policy",
        "scripts.test_run117_production_hardening",
        "scripts.test_run118_generation_hardening",
        "scripts.test_run120_dossier_repair_hardening",
        "scripts.test_run121_capacity_aware_planning",
        "scripts.test_run122_effective_capacity_admission",
        "scripts.test_run123_budget_closure",
        "scripts.test_run123_planning_latency_hardening",
        "scripts.test_run124_terminal_provider_recovery",
        "scripts.test_run125_capacity_routing_closure",
        "scripts.test_run126_capacity_snapshot_closure",
        "scripts.test_run127_runtime_composition",
        "scripts.test_run128_rate_limit_ownership",
        "scripts.test_provider_retry_ownership",
        "scripts.test_retry_after_policy",
        "scripts.test_reliability_failure_matrix",
    ),
    "P2": (
        "scripts.test_voice_mesh",
        "scripts.test_piper_chunking",
        "scripts.test_short_voice_v2",
        "scripts.test_audio_semantic_integrity",
        "scripts.test_audio_mastering_runtime_contract",
        "scripts.test_groq_audio_audit",
    ),
    "P3": (
        "scripts.test_media_trust_boundary_v2",
        "scripts.test_opening_feasibility_guard",
        "scripts.test_run108_stock_candidate_isolation",
        "scripts.test_security_v1_live_binding",
        "scripts.test_m7_live_binding",
        "scripts.test_m8_live_binding",
        "scripts.test_m9_live_binding",
        "scripts.test_m10_live_binding",
        "scripts.test_m11_live_binding",
        "scripts.test_narrative_music_dynamics",
        "scripts.test_narrative_music_dynamics_ffmpeg",
        "scripts.test_native_short_planner_router",
        "scripts.test_shorts_production_binding",
        "scripts.test_sibling_short_orchestration",
    ),
    "P4": (
        "scripts.test_final_master_qc",
        "scripts.test_final_master_qc_ffmpeg",
        "scripts.test_final_master_qc_timeout",
    ),
    "P5": (
        "scripts.test_gold_enforce_phase4",
        "scripts.test_gold_thumbnail_budget",
        "scripts.test_packaging_artifact_delivery_p0e",
    ),
    "P6": (
        "scripts.test_unified_delivery",
        "scripts.test_unified_delivery_canonical",
        "scripts.test_release_transaction",
        "scripts.test_analytics_live_observability",
        "scripts.test_analytics_observer_status",
    ),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_sha(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _run_tests(phase: str) -> list[str]:
    modules = PHASE_TESTS[phase]
    command = [sys.executable, "-m", "unittest", *modules, "-v"]
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", f"{ENGINE_ROOT / 'src'}:{ROOT}")
    history = Path(env.get("RUNNER_TEMP") or "/tmp") / "isco-stage-ladder" / f"{phase.lower()}-history.json"
    history.parent.mkdir(parents=True, exist_ok=True)
    env["ISCO_HISTORY_PATH"] = str(history)
    subprocess.run(command, cwd=ROOT, env=env, check=True)
    return list(modules)


def _phase_evidence(phase: str, evidence_dir: Path, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "status": "pass",
        "runner_sha": _git_sha(ROOT),
        "engine_sha": _git_sha(ENGINE_ROOT),
        "tests": list(PHASE_TESTS[phase]),
    }
    payload.update(extra)
    _write_json(evidence_dir / f"{phase}.json", payload)
    print(f"STAGE_LADDER {phase} PASS")
    return payload


def run_contract_phase(phase: str, evidence_dir: Path) -> dict[str, Any]:
    _run_tests(phase)
    return _phase_evidence(phase, evidence_dir)


def _baseline_identity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"{BASELINE_TAG} baseline asset is missing: {path}")
    size = path.stat().st_size
    digest = _sha256(path)
    if size != BASELINE_SIZE:
        raise RuntimeError(f"{BASELINE_TAG} size mismatch: {size} != {BASELINE_SIZE}")
    if digest != BASELINE_SHA256:
        raise RuntimeError(f"{BASELINE_TAG} SHA256 mismatch: {digest}")
    return {
        "release_tag": BASELINE_TAG,
        "asset": BASELINE_ASSET,
        "size_bytes": size,
        "sha256": digest,
    }


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError("video-50 baseline has invalid duration")
    return duration


def _prepare_staging(baseline: Path, staging: Path) -> float:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    final = staging / "final.mp4"
    try:
        os.link(baseline, final)
    except OSError:
        shutil.copy2(baseline, final)
    duration = _media_duration(final)
    _write_json(staging / "plan.json", {"format": "film", "topic": "video-50 known-good baseline replay"})
    _write_json(staging / "quality-final.json", {"format": "film", "status": "baseline_replay"})
    _write_json(
        staging / "visual-timeline.json",
        {
            "status": "baseline_replay",
            "duration_seconds": max(1.0, duration - 0.1),
            "source": BASELINE_TAG,
        },
    )
    return duration


def run_p4(evidence_dir: Path, baseline: Path, staging: Path) -> dict[str, Any]:
    _run_tests("P4")
    identity = _baseline_identity(baseline)
    duration = _prepare_staging(baseline, staging)
    before = _sha256(staging / "final.mp4")

    from scripts.final_master_qc import run_final_master_qc

    report = run_final_master_qc(staging)
    after = _sha256(staging / "final.mp4")
    if report.get("status") != "pass" or report.get("full_decode_ok") is not True:
        raise RuntimeError("P4 current Final Master QC did not pass video-50")
    if before != after or after != BASELINE_SHA256:
        raise RuntimeError("P4 mutated the known-good baseline media")
    return _phase_evidence(
        "P4",
        evidence_dir,
        baseline=identity,
        staging_dir=str(staging),
        duration_seconds=round(duration, 3),
        current_final_master_qc={
            "status": report.get("status"),
            "full_decode_ok": report.get("full_decode_ok"),
            "blocking_findings": report.get("blocking_findings"),
            "stream_contract": report.get("stream_contract"),
        },
        final_sha256_before=before,
        final_sha256_after=after,
    )


def run_p5(evidence_dir: Path, staging: Path) -> dict[str, Any]:
    _run_tests("P5")
    final = staging / "final.mp4"
    if _sha256(final) != BASELINE_SHA256:
        raise RuntimeError("P5 requires the unchanged video-50 P4 staging media")

    from isco_video_agent.ai_budget import BudgetLedger
    import scripts.gold_enforce_phase4 as gold

    history = staging / "stage-ladder-history.json"
    _write_json(history, {"productions": [{"id": "video-50-stage-ladder"}]})
    plan_object = type("StageLadderPlan", (), {"format": "film"})()

    def recorded_thumbnail_builder(**_kwargs: Any) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        for index in range(1, 4):
            name = f"thumbnail-{index}.jpg"
            (staging / name).write_bytes(b"stage-ladder-jpeg-evidence" * 200)
            candidates.append(
                {
                    "candidate_id": f"video50-{index}",
                    "file": name,
                    "photo_provider": "pexels",
                    "photo_id": 5000 + index,
                    "experiment_slot": chr(64 + index),
                    "title_ar": f"Baseline title {index}",
                    "text_ar": f"Baseline {index}",
                }
            )
        package = {"status": "ready", "candidates": candidates, "stage_ladder_recorded_boundary": True}
        _write_json(staging / "thumbnail-plan.json", package)
        return package

    def recorded_rights(_output_dir: Path, package: dict[str, Any]) -> dict[str, Any]:
        rights = {
            "visuals": [{"provider": "pexels", "provider_asset_id": 50}],
            "thumbnails": [
                {
                    "provider": "pexels",
                    "provider_asset_id": 5000 + index,
                    "output_file": f"thumbnail-{index}.jpg",
                    "license_url": "https://www.pexels.com/license/",
                }
                for index in range(1, 4)
            ],
        }
        _write_json(staging / "rights-manifest.json", rights)
        return rights

    def recorded_critic(**_kwargs: Any) -> dict[str, Any]:
        return {
            "status": "pass",
            "hard_blocks": [],
            "model_review": {"status": "pass", "summary": "recorded stage-ladder decision"},
        }

    before = _sha256(final)
    env = dict(os.environ)
    env["ISCO_HISTORY_PATH"] = str(history)
    with patch.dict(os.environ, env, clear=False), patch.object(
        gold, "_output_key", return_value="stage-ladder/video-50/final.mp4"
    ), patch.object(gold, "_plan_from_json", return_value=plan_object), patch.object(
        gold, "build_budgeted_thumbnail_package", side_effect=recorded_thumbnail_builder
    ), patch.object(gold, "_augment_rights_budget_aware", side_effect=recorded_rights), patch.object(
        gold, "_run_final_critic", side_effect=recorded_critic
    ), patch.object(gold, "mark_production_accepted", return_value=True), patch.object(
        gold, "remove_production_record"
    ), patch.object(gold, "_sync_state_snapshot"):
        _, critic, report = gold.run_gold_enforce_phase4(
            output_dir=staging,
            gemini="stage-ladder-recorded-boundary",
            pexels="stage-ladder-recorded-boundary",
            ledger=BudgetLedger("film", enforce=True),
        )

    after = _sha256(final)
    if before != after or after != BASELINE_SHA256:
        raise RuntimeError("P5 Gold changed video-50 final.mp4")
    if report.get("gold", {}).get("accepted") is not True:
        raise RuntimeError("P5 Gold did not accept the deterministic replay")
    if report.get("same_render", {}).get("artifact_divergence") is not False:
        raise RuntimeError("P5 Gold same-render invariant failed")
    if critic.get("status") != "pass":
        raise RuntimeError("P5 recorded Gold critic boundary did not pass")
    return _phase_evidence(
        "P5",
        evidence_dir,
        final_sha256_before=before,
        final_sha256_after=after,
        gold_report={
            "accepted": report.get("gold", {}).get("accepted"),
            "same_render": report.get("same_render"),
            "release_authority": report.get("release_authority"),
            "external_boundary": "recorded_deterministic_no_provider_call",
        },
    )


def run_p6(evidence_dir: Path, staging: Path) -> dict[str, Any]:
    _run_tests("P6")
    from scripts.packaging_delivery_contract import validate_packaging_delivery
    from scripts.unified_delivery import build_delivery_manifest

    if _sha256(staging / "final.mp4") != BASELINE_SHA256:
        raise RuntimeError("P6 requires unchanged video-50 media")
    _write_json(staging / "production-manifest.json", {"format": "film", "stage_ladder": True})
    package = validate_packaging_delivery(staging)
    manifest = build_delivery_manifest(
        staging,
        repository="mymusa79-tech/Isco-Video-Runner",
        release_tag=None,
    )
    if manifest.get("release_state") != "staged":
        raise RuntimeError("P6 must remain staged; Ladder cannot publish")
    if manifest.get("publication_performed") is not False:
        raise RuntimeError("P6 attempted or claimed publication")
    if manifest.get("partial_delivery_allowed") is not False:
        raise RuntimeError("P6 unexpectedly allows partial delivery")
    if len(manifest.get("title_thumbnail_pairs") or []) != 3:
        raise RuntimeError("P6 long-form package does not contain exactly three packaging pairs")
    return _phase_evidence(
        "P6",
        evidence_dir,
        delivery={
            "release_state": manifest.get("release_state"),
            "publication_performed": manifest.get("publication_performed"),
            "partial_delivery_allowed": manifest.get("partial_delivery_allowed"),
            "title_thumbnail_pair_count": len(manifest.get("title_thumbnail_pairs") or []),
            "validated_assets": sorted(path.name for path in package.values()),
            "release_transaction": "validated_by_current_dry_run_regression_only",
        },
    )


def _expand_run_spec(value: str) -> set[int]:
    value = str(value).strip()
    if "-" not in value:
        return {int(value)}
    left, right = value.split("-", 1)
    return set(range(int(left), int(right) + 1))


def _load_register() -> dict[str, Any]:
    data = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("family closure register root must be an object")
    return data


def certify(evidence_dir: Path, output: Path) -> dict[str, Any]:
    register = _load_register()
    evidence: dict[str, dict[str, Any]] = {}
    expected_runner = _git_sha(ROOT)
    expected_engine = _git_sha(ENGINE_ROOT)
    for phase in PHASES:
        path = evidence_dir / f"{phase}.json"
        if not path.is_file():
            raise RuntimeError(f"Stage Ladder missing {phase} evidence")
        item = json.loads(path.read_text(encoding="utf-8"))
        if item.get("status") != "pass":
            raise RuntimeError(f"Stage Ladder {phase} is not Green")
        if item.get("runner_sha") != expected_runner or item.get("engine_sha") != expected_engine:
            raise RuntimeError(f"Stage Ladder {phase} evidence belongs to a different source revision")
        evidence[phase] = item

    window = register["historical_window"]
    expected_runs = set(range(int(window["first_run"]), int(window["last_run"]) + 1))
    cohort_runs: set[int] = set()
    for cohort in register.get("audit_cohorts") or []:
        cohort_runs.update(_expand_run_spec(str(cohort["runs"])))
    if cohort_runs != expected_runs:
        missing = sorted(expected_runs - cohort_runs)
        extra = sorted(cohort_runs - expected_runs)
        raise RuntimeError(f"historical audit cohorts do not exactly cover Run51-130 missing={missing} extra={extra}")

    family_results: list[dict[str, Any]] = []
    for family in register.get("families") or []:
        required = list(family.get("required_phases") or [])
        if not required or any(phase not in evidence for phase in required):
            raise RuntimeError(f"family {family.get('id')} lacks current Ladder phase proof")
        contracts = list(family.get("contracts") or [])
        if not contracts:
            raise RuntimeError(f"family {family.get('id')} has no executable contracts")
        family_results.append(
            {
                "id": family["id"],
                "name": family["name"],
                "status": "closed_on_certified_sha",
                "required_phases": required,
                "historical_runs": family.get("historical_runs"),
                "contracts": contracts,
            }
        )

    baseline = evidence["P4"].get("baseline") or {}
    if baseline.get("sha256") != BASELINE_SHA256 or baseline.get("size_bytes") != BASELINE_SIZE:
        raise RuntimeError("P4 evidence is not bound to the exact video-50 baseline")

    certificate = {
        "schema_version": 1,
        "status": "green",
        "runner_sha": expected_runner,
        "engine_sha": expected_engine,
        "known_good_baseline": baseline,
        "phases": {phase: {"status": "pass", "evidence": f"{phase}.json"} for phase in PHASES},
        "historical_run_coverage": {"first_run": 51, "last_run": 130, "complete": True},
        "families": family_results,
        "production_dispatch_performed": False,
        "release_publication_performed": False,
        "certification_rule": "all_P0_through_P6_same_runner_sha_same_engine_sha",
    }
    _write_json(output, certificate)
    print(
        "PRODUCTION_STAGE_LADDER GREEN "
        f"runner={expected_runner} engine={expected_engine} families={len(family_results)}"
    )
    return certificate


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    phase_parser = sub.add_parser("phase")
    phase_parser.add_argument("phase", choices=PHASES)
    phase_parser.add_argument("--evidence-dir", required=True, type=Path)
    phase_parser.add_argument("--baseline", type=Path)
    phase_parser.add_argument("--staging", type=Path)

    cert_parser = sub.add_parser("certify")
    cert_parser.add_argument("--evidence-dir", required=True, type=Path)
    cert_parser.add_argument("--output", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "certify":
        certify(args.evidence_dir.resolve(), args.output.resolve())
        return 0

    evidence_dir = args.evidence_dir.resolve()
    phase = args.phase
    if phase in {"P0", "P1", "P2", "P3"}:
        run_contract_phase(phase, evidence_dir)
    elif phase == "P4":
        if args.baseline is None or args.staging is None:
            parser.error("P4 requires --baseline and --staging")
        run_p4(evidence_dir, args.baseline.resolve(), args.staging.resolve())
    elif phase == "P5":
        if args.staging is None:
            parser.error("P5 requires --staging")
        run_p5(evidence_dir, args.staging.resolve())
    elif phase == "P6":
        if args.staging is None:
            parser.error("P6 requires --staging")
        run_p6(evidence_dir, args.staging.resolve())
    else:  # pragma: no cover
        raise AssertionError(phase)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
