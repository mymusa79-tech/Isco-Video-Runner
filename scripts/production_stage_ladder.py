from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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
BASELINE_SIZE = 239_012_306
BASELINE_SHA256 = "143abdfb7b55f2269cab2c4634b68e7a99a5581ac08eceeccfb8cdda2ffdc742"
RUN_TEST_PATTERN = re.compile(r"^test_run(\d+)_.*\.py$")

PHASE_TESTS: dict[str, tuple[str, ...]] = {
    "P0": (
        "scripts.test_environment_preflight",
        "scripts.test_provider_preflight",
        "scripts.test_preproduction_contract",
        "scripts.test_crossref_reliability",
        "scripts.test_persistent_memory",
        "scripts.test_persistent_memory_migration_epoch",
        "scripts.test_checkpoint_namespace_guard",
        "scripts.test_run129_hermetic_approved_brief",
        "scripts.test_run129_production_test_isolation_contract",
        "scripts.test_run130_explicit_runtime_phase",
        "scripts.test_run130_runtime_authority_contract",
        "scripts.test_run179_engine_pin_single_source",
        "scripts.test_run188_short_capability_ownership",
        "scripts.test_runtime_closure",
        "scripts.test_runtime_closure_idempotence_run102",
        "scripts.test_run103_runtime_marker_preservation",
        "scripts.test_state_persistence_strict",
    ),
    "P1": (
        "scripts.test_planner_quality_guard",
        "scripts.test_planning_provider_reliability_v2",
        "scripts.test_bounded_output_recovery",
        "scripts.test_schema_repair_policy",
        "scripts.test_planning_capacity_headroom",
        "scripts.test_planning_stage_contract",
        "scripts.test_run117_production_hardening",
        "scripts.test_run118_generation_hardening",
        "scripts.test_run120_dossier_repair_hardening",
        "scripts.test_run121_capacity_aware_planning",
        "scripts.test_run122_effective_capacity_admission",
        "scripts.test_run123_budget_closure",
        "scripts.test_run123_planning_latency_hardening",
        "scripts.test_run124_terminal_provider_recovery",
        "scripts.test_run125_capacity_routing_closure",
        "scripts.test_run125_terminal_retry_budget",
        "scripts.test_run126_capacity_snapshot_closure",
        "scripts.test_run127_runtime_composition",
        "scripts.test_run128_rate_limit_ownership",
        "scripts.test_run157_text_audit_pool_isolation",
        "scripts.test_run164_producer_short_repair_lifecycle",
        "scripts.test_run167_text_audit_capacity_ownership",
        "scripts.test_run168_provider_visible_semantics",
        "scripts.test_run170_planning_contract_composition",
        "scripts.test_run179_short_representation_contract",
        "scripts.test_run180_micro_story_contract_closure",
        "scripts.test_run180_short_representation_metadata_authority",
        "scripts.test_run180_short_template_representation_lifecycle",
        "scripts.test_run181_groq_readiness_budget",
        "scripts.test_run187_moment_duration_contract_closure",
        "scripts.test_run189_micro_story_contract_alignment",
        "scripts.test_run191_short_repair_composition_invariants",
        "scripts.test_run191_tone_audit_representation_bridge",
        "scripts.test_p0_2_budget_enforcement",
        "scripts.test_budget_wire_attempts",
        "scripts.test_provider_retry_ownership",
        "scripts.test_retry_after_policy",
        "scripts.test_reliability_failure_matrix",
    ),
    "P2": (
        "scripts.test_voice_mesh",
        "scripts.test_piper_chunking",
        "scripts.test_short_voice_v2",
        "scripts.test_run192_short_voice_feasibility",
        "scripts.test_audio_semantic_integrity",
        "scripts.test_audio_mastering_runtime_contract",
        "scripts.test_audio_producer_repair_lifecycle",
        "scripts.test_groq_audio_audit",
    ),
    "P3": (
        "scripts.test_media_trust_boundary_v2",
        "scripts.test_run92_opening_feasibility_guard",
        "scripts.test_run108_stock_candidate_isolation",
        "scripts.test_run169_visual_retrieval_truth",
        "scripts.test_run181_vision_provider_health_mesh",
        "scripts.test_run181_vision_scope_budget",
        "scripts.test_run183_visual_retrieval_closure",
        "scripts.test_run184_qr_confirmation_closure",
        "scripts.test_run184_qr_runtime_scope",
        "scripts.test_run185_semantic_visual_adjudication",
        "scripts.test_vision_provider_reliability",
        "scripts.test_vision_stage_contract_v2",
        "scripts.test_vision_stage_transport_v2",
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
        "scripts.test_audio_producer_final_certificate",
        "scripts.test_final_master_acceptance_v2",
        "scripts.test_final_master_qc_contract_shape",
        "scripts.test_final_master_acceptance_contract_registration",
        "scripts.test_run186_format_aware_final_master_qc",
        "scripts.test_f24_family_contract",
        "scripts.test_f24_no_production_dispatch",
    ),
    "P5": (
        "scripts.test_gold_enforce_phase4",
        "scripts.test_thumbnail_pixabay_bridge_p0d",
        "scripts.test_packaging_artifact_delivery_p0e",
    ),
    "P6": (
        "scripts.test_unified_delivery",
        "scripts.test_unified_delivery_canonical",
        "scripts.test_release_transaction",
        "scripts.test_release_reconciliation_contracts",
        "scripts.test_release_reconciliation_journal",
        "scripts.test_release_terminal_provenance_closure",
        "scripts.test_telegram_publish_gate",
        "scripts.test_telegram_release_approval",
        "scripts.test_telegram_release_identity",
        "scripts.test_telegram_final_notify",
        "scripts.test_channel_os_youtube_manual_only",
        "scripts.test_delivery_acceptance_v2",
        "scripts.test_f25_family_contract",
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
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ).stdout.strip()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _run_tests(phase: str) -> None:
    base_env = dict(os.environ)
    base_env.setdefault("PYTHONPATH", f"{ENGINE_ROOT / 'src'}:{ROOT}")
    phase_root = Path(base_env.get("RUNNER_TEMP") or "/tmp") / "isco-stage-ladder" / phase.lower()
    phase_root.mkdir(parents=True, exist_ok=True)

    # Contract suites install process-global wrappers around Engine/Runner modules.
    # A Stage Ladder phase must certify each declared contract, not accidental state
    # inherited from the suite that ran immediately before it. Give every test module
    # a fresh interpreter and an isolated history file while keeping the same exact
    # Runner+Engine checkout pair for the whole phase.
    for index, test_module in enumerate(PHASE_TESTS[phase], start=1):
        env = dict(base_env)
        module_name = test_module.rsplit(".", 1)[-1]
        env["ISCO_HISTORY_PATH"] = str(
            phase_root / f"{index:02d}-{module_name}-history.json"
        )
        subprocess.run(
            [sys.executable, "-m", "unittest", test_module, "-v"],
            cwd=ROOT, env=env, check=True,
        )


def _record(phase: str, evidence_dir: Path, **extra: Any) -> dict[str, Any]:
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


def run_contract_phase(phase: str, evidence_dir: Path) -> None:
    _run_tests(phase)
    _record(phase, evidence_dir)


def _verify_baseline(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError("video-50 baseline final.mp4 is missing")
    size = path.stat().st_size
    digest = _sha256(path)
    if size != BASELINE_SIZE or digest != BASELINE_SHA256:
        raise RuntimeError(f"video-50 identity mismatch size={size} sha256={digest}")
    return {"release_tag": BASELINE_TAG, "asset": "final.mp4", "size_bytes": size, "sha256": digest}


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60,
    )
    value = float(result.stdout.strip())
    if value <= 0:
        raise RuntimeError("video-50 duration is invalid")
    return value


def _prepare_staging(baseline: Path, staging: Path) -> float:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    final = staging / "final.mp4"
    try:
        os.link(baseline, final)
    except OSError:
        shutil.copy2(baseline, final)
    duration = _duration(final)
    _write_json(staging / "plan.json", {"format": "film", "topic": "video-50 known-good baseline replay"})
    _write_json(staging / "quality-final.json", {"format": "film", "status": "baseline_replay"})
    _write_json(staging / "visual-timeline.json", {
        "status": "baseline_replay", "duration_seconds": max(1.0, duration - 0.1), "source": BASELINE_TAG,
    })
    return duration


def run_p4(evidence_dir: Path, baseline: Path, staging: Path) -> None:
    _run_tests("P4")
    identity = _verify_baseline(baseline)
    duration = _prepare_staging(baseline, staging)
    before = _sha256(staging / "final.mp4")
    from scripts.final_qc_observer_durability import run_final_master_qc_durable
    report = run_final_master_qc_durable(staging)
    after = _sha256(staging / "final.mp4")
    acceptance = report.get("acceptance_contract") if isinstance(report, dict) else None
    if report.get("status") != "pass" or report.get("full_decode_ok") is not True:
        raise RuntimeError("P4 current Final Master QC did not pass video-50")
    if not isinstance(acceptance, dict) or acceptance.get("contract_id") != "final.master.acceptance.v2":
        raise RuntimeError("P4 video-50 did not receive Final Master Acceptance V2")
    if acceptance.get("decision") != "pass":
        raise RuntimeError("P4 Final Master Acceptance V2 did not pass video-50")
    if (acceptance.get("sources") or {}).get("final", {}).get("sha256") != BASELINE_SHA256:
        raise RuntimeError("P4 Final Master Acceptance V2 is not bound to exact video-50 bytes")
    if (report.get("upload_conformance") or {}).get("decision") != "pass":
        raise RuntimeError("P4 upload conformance did not pass video-50")
    if before != after or after != BASELINE_SHA256:
        raise RuntimeError("P4 changed video-50 media")
    _record(
        "P4", evidence_dir, baseline=identity, duration_seconds=round(duration, 3),
        current_final_master_qc={
            "status": report.get("status"), "full_decode_ok": report.get("full_decode_ok"),
            "blocking_findings": report.get("blocking_findings"), "stream_contract": report.get("stream_contract"),
            "acceptance_contract_id": acceptance.get("contract_id"),
            "acceptance_decision": acceptance.get("decision"),
            "certified_final_sha256": (acceptance.get("sources") or {}).get("final", {}).get("sha256"),
            "upload_conformance": (report.get("upload_conformance") or {}).get("decision"),
        }, final_sha256_before=before, final_sha256_after=after,
    )


def run_p5(evidence_dir: Path, staging: Path) -> None:
    _run_tests("P5")
    final = staging / "final.mp4"
    if _sha256(final) != BASELINE_SHA256:
        raise RuntimeError("P5 requires unchanged P4 video-50 media")
    from isco_video_agent.ai_budget import BudgetLedger
    import scripts.gold_enforce_phase4 as gold

    history = staging / "stage-ladder-history.json"
    _write_json(history, {"productions": [{"id": "video-50-stage-ladder"}]})
    plan_object = type("StageLadderPlan", (), {"format": "film"})()

    def recorded_builder(**_kwargs: Any) -> dict[str, Any]:
        candidates = []
        for index in range(1, 4):
            name = f"thumbnail-{index}.jpg"
            (staging / name).write_bytes(b"stage-ladder-jpeg-evidence" * 200)
            candidates.append({
                "candidate_id": f"video50-{index}", "file": name, "photo_provider": "pexels",
                "photo_id": 5000 + index, "experiment_slot": chr(64 + index),
                "title_ar": f"Baseline title {index}", "text_ar": f"Baseline {index}",
            })
        package = {"status": "ready", "candidates": candidates, "stage_ladder_recorded_boundary": True}
        _write_json(staging / "thumbnail-plan.json", package)
        return package

    def recorded_rights(_output_dir: Path, _package: dict[str, Any]) -> dict[str, Any]:
        rights = {
            "visuals": [{"provider": "pexels", "provider_asset_id": 50}],
            "thumbnails": [{
                "provider": "pexels", "provider_asset_id": 5000 + index,
                "output_file": f"thumbnail-{index}.jpg", "license_url": "https://www.pexels.com/license/",
            } for index in range(1, 4)],
        }
        _write_json(staging / "rights-manifest.json", rights)
        return rights

    def recorded_critic(**_kwargs: Any) -> dict[str, Any]:
        return {"status": "pass", "hard_blocks": [], "model_review": {
            "status": "pass", "summary": "recorded stage-ladder decision"}}

    before = _sha256(final)
    with patch.dict(os.environ, {**os.environ, "ISCO_HISTORY_PATH": str(history)}, clear=False), patch.object(
        gold, "_output_key", return_value="stage-ladder/video-50/final.mp4"
    ), patch.object(gold, "_plan_from_json", return_value=plan_object), patch.object(
        gold, "build_budgeted_thumbnail_package", side_effect=recorded_builder
    ), patch.object(gold, "_augment_rights_budget_aware", side_effect=recorded_rights), patch.object(
        gold, "_run_final_critic", side_effect=recorded_critic
    ), patch.object(gold, "mark_production_accepted", return_value=True), patch.object(
        gold, "remove_production_record"
    ), patch.object(gold, "_sync_state_snapshot"):
        _, critic, report = gold.run_gold_enforce_phase4(
            output_dir=staging, gemini="recorded-boundary", pexels="recorded-boundary",
            ledger=BudgetLedger("film", enforce=True),
        )
    after = _sha256(final)
    if before != after or after != BASELINE_SHA256:
        raise RuntimeError("P5 Gold changed video-50 final.mp4")
    if report.get("gold", {}).get("accepted") is not True or critic.get("status") != "pass":
        raise RuntimeError("P5 deterministic Gold replay did not pass")
    if report.get("same_render", {}).get("artifact_divergence") is not False:
        raise RuntimeError("P5 Gold same-render invariant failed")
    _record("P5", evidence_dir, final_sha256_before=before, final_sha256_after=after, gold_report={
        "accepted": True, "same_render": report.get("same_render"),
        "release_authority": report.get("release_authority"),
        "external_boundary": "recorded_deterministic_no_provider_call",
    })


def run_p6(evidence_dir: Path, staging: Path) -> None:
    _run_tests("P6")
    from scripts.packaging_delivery_contract import validate_packaging_delivery
    from scripts.unified_delivery import build_delivery_manifest
    if _sha256(staging / "final.mp4") != BASELINE_SHA256:
        raise RuntimeError("P6 requires unchanged video-50 media")
    _write_json(staging / "production-manifest.json", {"format": "film", "stage_ladder": True})
    package = validate_packaging_delivery(staging)
    manifest = build_delivery_manifest(
        staging, repository="mymusa79-tech/Isco-Video-Runner", release_tag=None,
    )
    if manifest.get("release_state") != "staged" or manifest.get("publication_performed") is not False:
        raise RuntimeError("P6 must remain staged and unpublished")
    if manifest.get("partial_delivery_allowed") is not False or len(manifest.get("title_thumbnail_pairs") or []) != 3:
        raise RuntimeError("P6 delivery contract failed")
    if manifest.get("primary_video_sha256") != BASELINE_SHA256:
        raise RuntimeError("P6 delivery is not bound to the P4-certified video-50 bytes")
    _record("P6", evidence_dir, delivery={
        "release_state": "staged", "publication_performed": False, "partial_delivery_allowed": False,
        "title_thumbnail_pair_count": 3, "validated_assets": sorted(path.name for path in package.values()),
        "certified_final_sha256": manifest.get("primary_video_sha256"),
        "release_transaction": "validated_by_current_dry_run_regression_only",
    })


def _expand(spec: str) -> set[int]:
    spec = str(spec).strip()
    if "-" not in spec:
        return {int(spec)}
    left, right = spec.split("-", 1)
    return set(range(int(left), int(right) + 1))


def _named_run_regressions() -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for path in sorted((ROOT / "scripts").glob("test_run*_*.py")):
        match = RUN_TEST_PATTERN.match(path.name)
        if match is None:
            continue
        result.setdefault(int(match.group(1)), set()).add(f"scripts.{path.stem}")
    return result


def certify(evidence_dir: Path, output: Path) -> None:
    register = json.loads(REGISTER_PATH.read_text(encoding="utf-8"))
    runner_sha, engine_sha = _git_sha(ROOT), _git_sha(ENGINE_ROOT)
    evidence: dict[str, dict[str, Any]] = {}
    for phase in PHASES:
        item = json.loads((evidence_dir / f"{phase}.json").read_text(encoding="utf-8"))
        if item.get("status") != "pass" or item.get("runner_sha") != runner_sha or item.get("engine_sha") != engine_sha:
            raise RuntimeError(f"{phase} evidence is not Green on the exact current source pair")
        evidence[phase] = item

    window = register["historical_window"]
    first_run = int(window["first_run"])
    last_run = int(window["last_run"])
    expected = set(range(first_run, last_run + 1))
    cohort_membership: dict[int, list[str]] = {}
    for cohort in register.get("audit_cohorts") or []:
        cohort_id = str(cohort.get("id") or "unknown")
        for run_number in _expand(cohort["runs"]):
            cohort_membership.setdefault(run_number, []).append(cohort_id)
    covered = set(cohort_membership)
    duplicates = {run: ids for run, ids in cohort_membership.items() if len(ids) != 1}
    if covered != expected or duplicates:
        raise RuntimeError(
            f"audit cohorts do not exactly-once cover Run{first_run}-{last_run} "
            f"missing={sorted(expected-covered)} extra={sorted(covered-expected)} duplicates={duplicates}"
        )

    executed_tests = {
        str(test_name)
        for phase in PHASES
        for test_name in (evidence[phase].get("tests") or [])
    }
    families = []
    for family in register.get("families") or []:
        required = list(family.get("required_phases") or [])
        if not required or any(phase not in evidence for phase in required):
            raise RuntimeError(f"family {family.get('id')} lacks current phase evidence")
        contracts = list(family.get("contracts") or [])
        if not contracts:
            raise RuntimeError(f"family {family.get('id')} has no executable contracts")
        missing_contracts = sorted(set(contracts) - executed_tests)
        if missing_contracts:
            raise RuntimeError(
                f"family {family.get('id')} declares contracts not executed by current Stage Ladder: "
                f"{missing_contracts}"
            )
        family_runs: set[int] = set()
        for spec in family.get("historical_runs") or []:
            family_runs.update(_expand(spec))
        if not family_runs or not family_runs.issubset(expected):
            raise RuntimeError(
                f"family {family.get('id')} references runs outside certified window: "
                f"{sorted(family_runs-expected)}"
            )
        families.append({
            "id": family["id"], "name": family["name"], "status": "closed_on_certified_sha",
            "required_phases": required, "historical_runs": family.get("historical_runs"),
            "contracts": contracts, "contracts_executed_on_sha": True,
        })

    guard = register.get("forward_guard") or {}
    enforcement_from = int(guard.get("named_run_contract_enforcement_from") or last_run + 1)
    named_regressions = {
        run: modules
        for run, modules in _named_run_regressions().items()
        if run >= enforcement_from
    }
    max_named_run = max(named_regressions, default=enforcement_from - 1)
    if guard.get("require_window_not_behind_named_regressions") is True and max_named_run > last_run:
        raise RuntimeError(
            f"family closure register is stale: last_run={last_run} max_named_regression_run={max_named_run}"
        )
    declared_contracts = {
        str(contract)
        for family in families
        for contract in family.get("contracts") or []
    }
    orphaned = {
        run: sorted(modules - declared_contracts)
        for run, modules in named_regressions.items()
        if run <= last_run and modules - declared_contracts
    }
    if guard.get("require_every_named_regression_declared") is True and orphaned:
        raise RuntimeError(f"named production regressions are not declared by a family: {orphaned}")

    baseline = evidence["P4"].get("baseline") or {}
    if baseline.get("sha256") != BASELINE_SHA256 or baseline.get("size_bytes") != BASELINE_SIZE:
        raise RuntimeError("P4 evidence is not bound to exact video-50")
    certificate = {
        "schema_version": 1, "status": "green", "runner_sha": runner_sha, "engine_sha": engine_sha,
        "known_good_baseline": baseline,
        "phases": {phase: {"status": "pass", "evidence": f"{phase}.json"} for phase in PHASES},
        "historical_run_coverage": {"first_run": first_run, "last_run": last_run, "complete": True},
        "named_run_contract_guard": {
            "enforcement_from": enforcement_from,
            "max_named_regression_run": max_named_run,
            "orphaned_contracts": {},
            "complete": True,
        },
        "families": families, "production_dispatch_performed": False, "release_publication_performed": False,
        "certification_rule": "all_P0_through_P6_same_runner_sha_same_engine_sha_all_family_contracts_executed_no_orphaned_named_run_regressions",
    }
    _write_json(output, certificate)
    print(
        f"PRODUCTION_STAGE_LADDER GREEN runner={runner_sha} engine={engine_sha} "
        f"runs={first_run}-{last_run} families={len(families)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    phase_p = sub.add_parser("phase")
    phase_p.add_argument("phase", choices=PHASES)
    phase_p.add_argument("--evidence-dir", required=True, type=Path)
    phase_p.add_argument("--baseline", type=Path)
    phase_p.add_argument("--staging", type=Path)
    cert_p = sub.add_parser("certify")
    cert_p.add_argument("--evidence-dir", required=True, type=Path)
    cert_p.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "certify":
        certify(args.evidence_dir.resolve(), args.output.resolve())
        return 0
    phase, evidence = args.phase, args.evidence_dir.resolve()
    if phase in {"P0", "P1", "P2", "P3"}:
        run_contract_phase(phase, evidence)
    elif phase == "P4":
        if args.baseline is None or args.staging is None:
            parser.error("P4 requires --baseline and --staging")
        run_p4(evidence, args.baseline.resolve(), args.staging.resolve())
    elif phase == "P5":
        if args.staging is None:
            parser.error("P5 requires --staging")
        run_p5(evidence, args.staging.resolve())
    elif phase == "P6":
        if args.staging is None:
            parser.error("P6 requires --staging")
        run_p6(evidence, args.staging.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())