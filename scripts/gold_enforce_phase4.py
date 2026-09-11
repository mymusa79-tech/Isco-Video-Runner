from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.anti_repetition import load_history
from isco_video_agent.gold_finalizer import finalize_gold_output
from isco_video_agent.learning import mark_production_accepted, remove_production_record
from isco_video_agent.production_pipeline import (
    _augment_rights,
    _output_key,
    _plan_from_json,
    _run_final_critic,
    _sync_state_snapshot,
)
from isco_video_agent.security import safe_error

from scripts.final_master_acceptance_v2 import require_final_master_acceptance
from scripts.gold_final_critic_text_fallback import gold_final_critic_text_fallback
from scripts.gold_shadow_phase2a import _fingerprint, _provider_attempt_total
from scripts.gold_thumbnail_budget import build_budgeted_thumbnail_package
from scripts.packaging_delivery_contract import (
    ACCEPTANCE_FILENAME,
    CONTRACT_ID as GOLD_PACKAGING_CONTRACT_ID,
    gold_packaging_acceptance_sha256,
    seal_gold_packaging_acceptance,
)
from scripts.qc_pending_checkpoint_v1 import capture_qc_pending_checkpoint
from scripts.qc_pending_resume_bundle_v1 import build_resume_bundle
from scripts.run123_budget_closure import enforcing_final_critic_as_p0
from scripts.telegram_progress import advance_stage
from scripts.viewer_quality_contract_v1 import enforce_viewer_quality_contract


# The Production V4 failure artifact already uploads `engine/output/*/short-*`
# recursively. `short-circuit` here means the Gold recovery short-circuit (not a
# YouTube Short): keeping the bundle under this stable transport namespace lets the
# exact source run carry its own immutable recovery bytes without a second renderer or
# a later cross-run artifact copy.
QC_PENDING_DIAGNOSTIC_BUNDLE_DIRNAME = "short-circuit-gold-resume-v2"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _persist_budget_snapshot(ledger: BudgetLedger, output_dir: Path) -> Path:
    """Atomically persist the exact in-memory AI ledger at the Gold boundary.

    The QC_PENDING resume bundle treats ``ai-budget.json`` as required provenance. A
    Gold provider can fail before the outer production exception handler gets a chance
    to flush the ledger, so the file must exist before the first Gold provider call and
    must be refreshed before a failure checkpoint is sealed. The temporary file is
    fsync'd and atomically replaced; a crash can therefore expose either the previous
    complete snapshot or the new complete snapshot, never a partially-written budget.
    """
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "ai-budget.json"
    temporary = root / ".ai-budget.json.tmp"
    try:
        ledger.write(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        try:
            directory_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


def _snapshot_pending_production_record(output_key: str) -> dict | None:
    """Capture the core history row before Gold's fail-closed cleanup can remove it."""
    try:
        data = load_history()
    except Exception:
        return None
    videos = data.get("videos") if isinstance(data, dict) else None
    if not isinstance(videos, list):
        return None
    for item in reversed(videos):
        if not isinstance(item, dict) or str(item.get("output") or "").strip() != output_key:
            continue
        if str(item.get("release_status") or "").strip() == "accepted_after_final_critic":
            return None
        return deepcopy(item)
    return None


def _augment_rights_budget_aware(output_dir: Path, package: dict) -> dict:
    """Preserve exact rights provenance for zero-AI final-render thumbnail fallback."""
    if package.get("budget_degraded") is not True:
        return _augment_rights(output_dir, package)

    engine_package = dict(package)
    engine_package["candidates"] = []
    rights = _augment_rights(output_dir, engine_package)
    inherited = list(rights.get("visuals") or [])
    if not inherited:
        raise RuntimeError("Budget-fallback thumbnails cannot inherit missing final-cut visual rights")

    records: list[dict] = []
    for candidate in package.get("candidates") or []:
        if not isinstance(candidate, dict):
            raise RuntimeError("Budget-fallback thumbnail candidate is malformed")
        file_name = str(candidate.get("file") or "").strip()
        asset_id = str(candidate.get("photo_id") or "").strip()
        if not file_name or not (Path(output_dir) / file_name).is_file() or not asset_id:
            raise RuntimeError("Budget-fallback thumbnail lacks exact derivative provenance")
        records.append(
            {
                "provider": "derived_final_render",
                "provider_asset_id": asset_id,
                "source_url": None,
                "creator": None,
                "creator_url": None,
                "license_url": None,
                "output_file": file_name,
                "source_file": "final.mp4",
                "source_timestamp_seconds": candidate.get("source_timestamp_seconds"),
                "rights_inheritance": "rights-manifest.visuals",
                "inherited_visual_rights_count": len(inherited),
            }
        )
    if len(records) != 3:
        raise RuntimeError("Budget-fallback packaging must bind exactly three thumbnail derivatives")
    rights["thumbnails"] = records
    rights["thumbnail_rights_mode"] = "derived_from_already_rights_cleared_final_render"
    (Path(output_dir) / "rights-manifest.json").write_text(
        json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return rights


def _read_viewer_quality_report(output_dir: Path) -> dict | None:
    path = Path(output_dir) / "viewer-quality-contract.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def run_gold_enforce_phase4(
    *,
    output_dir: Path,
    gemini: str,
    pexels: str,
    ledger: BudgetLedger,
    pixabay: str | None = None,
) -> tuple[object, dict, dict]:
    """Enforce Gold over the same P4-certified render and accept state only last.

    The enforcing Gold critic runs first. Viewer Quality then evaluates the exact same
    final bytes before packaging is sealed and before ``mark_production_accepted`` can
    mutate history. Any Viewer Quality failure therefore flows through the existing Gold
    rejection cleanup in ``finalize_gold_output`` instead of creating an accepted-but-
    unreleasable production state.
    """
    output_dir = Path(output_dir)
    final_path = output_dir / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Final video missing before Gold enforcement")
    final_sha_before = _sha256_file(final_path)
    output_key = _output_key(output_dir)
    pending_production_record = _snapshot_pending_production_record(output_key)

    p4_acceptance: dict | None = None
    qc_path = output_dir / "final-master-qc.json"
    if qc_path.is_file():
        p4_acceptance = require_final_master_acceptance(output_dir)
        certified_sha = str(
            p4_acceptance["acceptance_contract"]["sources"]["final"]["sha256"]
        )
        if certified_sha != final_sha_before:
            raise RuntimeError("Gold enforcement received bytes different from P4 certificate")

    # Recovery provenance must pre-exist before any Gold provider can fail. The same
    # snapshot is refreshed immediately after Gold returns/raises below so a deferred
    # bundle contains the exact provider attempts made by this Gold execution.
    _persist_budget_snapshot(ledger, output_dir)

    attempts_before = _provider_attempt_total(ledger)
    state_before = _fingerprint(Path(os.environ.get("ISCO_HISTORY_PATH", ""))) if os.environ.get("ISCO_HISTORY_PATH") else {
        "exists": None,
        "sha256": None,
        "observation_status": "not_configured",
    }
    critic_box: dict[str, dict] = {}
    packaging_acceptance_box: dict[str, dict] = {}

    def budgeted_builder(**kwargs):
        return build_budgeted_thumbnail_package(**kwargs, ledger=ledger, pixabay_key=pixabay)

    def enforced_critic(**kwargs):
        advance_stage("gold_vision")
        with enforcing_final_critic_as_p0(), gold_final_critic_text_fallback():
            critic = _run_final_critic(
                **kwargs,
                ledger=ledger,
                release_mode="enforce",
                report_dir=output_dir,
                task_prefix="GOLD_",
                task_kind="GOLD_FINAL_CRITIC",
            )
        critic_box["critic"] = critic
        if _sha256_file(final_path) != final_sha_before:
            raise RuntimeError("Gold enforcement detected final.mp4 mutation before Viewer Quality")

        plan = kwargs.get("plan")
        fmt = str(getattr(plan, "format", "") or "").strip().lower()
        if not fmt:
            raise RuntimeError("Gold enforcement lost format before Viewer Quality")
        advance_stage("viewer_quality")
        enforce_viewer_quality_contract(
            output_dir,
            fmt=fmt,
            critic=critic,
        )
        if _sha256_file(final_path) != final_sha_before:
            raise RuntimeError("Viewer Quality mutated final.mp4 before state acceptance")

        advance_stage("packaging")
        packaging_acceptance_box["acceptance"] = seal_gold_packaging_acceptance(
            output_dir,
            critic=critic,
        )
        return critic

    error: Exception | None = None
    plan = None
    critic: dict = {}
    try:
        plan, critic = finalize_gold_output(
            output_dir=output_dir,
            output_key=output_key,
            gemini=gemini,
            pexels=pexels,
            plan_from_json=_plan_from_json,
            build_thumbnail_package=budgeted_builder,
            augment_rights=_augment_rights_budget_aware,
            run_final_critic=enforced_critic,
            remove_production_record=remove_production_record,
            mark_production_accepted=mark_production_accepted,
            sync_state_snapshot=_sync_state_snapshot,
        )
    except Exception as exc:
        error = exc
        critic = critic_box.get("critic", critic)
    finally:
        _persist_budget_snapshot(ledger, output_dir)

    attempts_after = _provider_attempt_total(ledger)
    final_sha_after = _sha256_file(final_path) if final_path.is_file() else None
    state_after = _fingerprint(Path(os.environ.get("ISCO_HISTORY_PATH", ""))) if os.environ.get("ISCO_HISTORY_PATH") else {
        "exists": None,
        "sha256": None,
        "observation_status": "not_configured",
    }
    thumbnail_plan: dict = {}
    try:
        raw_thumbnail = json.loads((output_dir / "thumbnail-plan.json").read_text(encoding="utf-8"))
        if isinstance(raw_thumbnail, dict):
            thumbnail_plan = raw_thumbnail
    except Exception:
        pass
    packaging_acceptance = packaging_acceptance_box.get("acceptance")
    certificate_sha256 = None
    if isinstance(packaging_acceptance, dict) and (output_dir / ACCEPTANCE_FILENAME).is_file():
        try:
            certificate_sha256 = gold_packaging_acceptance_sha256(output_dir)
        except Exception:
            certificate_sha256 = None
    viewer_quality = _read_viewer_quality_report(output_dir)
    report = {
        "schema_version": 4,
        "phase": "4",
        "mode": "enforce",
        "release_authority": "gold",
        "single_render": True,
        "entrypoint_switched": False,
        "p4_acceptance": {
            "required_on_canonical_path": True,
            "present": p4_acceptance is not None,
            "contract_id": (
                p4_acceptance.get("acceptance_contract", {}).get("contract_id")
                if isinstance(p4_acceptance, dict)
                else None
            ),
            "certified_final_sha256": (
                p4_acceptance.get("acceptance_contract", {}).get("sources", {}).get("final", {}).get("sha256")
                if isinstance(p4_acceptance, dict)
                else None
            ),
        },
        "viewer_quality": {
            "required": True,
            "present": isinstance(viewer_quality, dict),
            "contract_id": viewer_quality.get("contract_id") if isinstance(viewer_quality, dict) else None,
            "verdict": viewer_quality.get("verdict") if isinstance(viewer_quality, dict) else None,
            "score_10": viewer_quality.get("viewer_score_10") if isinstance(viewer_quality, dict) else None,
            "minimum_score_10": viewer_quality.get("minimum_viewer_score_10") if isinstance(viewer_quality, dict) else None,
            "release_profile": viewer_quality.get("release_profile") if isinstance(viewer_quality, dict) else None,
            "evaluated_before_packaging_and_state_acceptance": True,
        },
        "packaging_acceptance": {
            "required": True,
            "present": isinstance(packaging_acceptance, dict),
            "contract_id": (
                packaging_acceptance.get("contract_id")
                if isinstance(packaging_acceptance, dict)
                else GOLD_PACKAGING_CONTRACT_ID
            ),
            "profile": (
                packaging_acceptance.get("profile")
                if isinstance(packaging_acceptance, dict)
                else None
            ),
            "certificate_file": ACCEPTANCE_FILENAME,
            "certificate_sha256": certificate_sha256,
            "embedded_certificate": packaging_acceptance if isinstance(packaging_acceptance, dict) else None,
            "sealed_after_viewer_quality": True,
            "sealed_before_state_acceptance": True,
        },
        "same_render": {
            "path": str(final_path),
            "sha256_before": final_sha_before,
            "sha256_after": final_sha_after,
            "artifact_divergence": final_sha_after != final_sha_before,
        },
        "gold": {
            "status": critic.get("status") if isinstance(critic, dict) else None,
            "hard_blocks": critic.get("hard_blocks", []) if isinstance(critic, dict) else [],
            "enforced": True,
            "accepted": error is None,
        },
        "state_observation": {
            "before": state_before,
            "after": state_after,
            "mutation_expected_on_success": True,
            "acceptance_is_terminal_mutation": True,
            "failure_cleanup_expected": error is not None,
            "pending_record_captured_before_gold": pending_production_record is not None,
        },
        "budget": {
            "same_ledger": True,
            "provider_attempts_before_gold": attempts_before,
            "provider_attempts_after_gold": attempts_after,
            "gold_provider_attempt_delta": max(0, attempts_after - attempts_before),
            "thumbnail_p2_safe_skip": thumbnail_plan.get("budget_degraded") is True,
            "thumbnail_provider_attempts_consumed": (
                (thumbnail_plan.get("budget_fallback") or {}).get("provider_attempts_consumed")
                if isinstance(thumbnail_plan.get("budget_fallback"), dict)
                else None
            ),
            "final_critic_priority": "P0",
        },
        "error": safe_error(error) if error is not None else None,
    }
    try:
        (output_dir / "gold-enforce-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    if error is not None:
        try:
            # Refresh once more immediately before checkpoint sealing. This is cheap and
            # makes the ordering explicit for future refactors: budget provenance first,
            # then QC_PENDING checkpoint, then immutable resume bundle.
            _persist_budget_snapshot(ledger, output_dir)
            checkpoint = capture_qc_pending_checkpoint(
                output_dir,
                error,
                production_record=pending_production_record,
                output_key=output_key,
            )
            if checkpoint is not None:
                bundle_dir = output_dir / QC_PENDING_DIAGNOSTIC_BUNDLE_DIRNAME
                manifest = build_resume_bundle(output_dir, bundle_dir)
                print(
                    "QC_PENDING recovery bundle sealed before workflow teardown: "
                    f"dir={bundle_dir.name} final_sha256={str(manifest.get('final_sha256') or '')[:12]}"
                )
        except Exception as checkpoint_exc:
            print(
                "QC_PENDING capture/bundle skipped without masking Gold failure "
                f"({type(checkpoint_exc).__name__}: {str(checkpoint_exc)[:180]})"
            )
        raise error
    if final_sha_after != final_sha_before:
        raise RuntimeError("Gold enforcement final.mp4 invariant failed after acceptance")
    if not isinstance(viewer_quality, dict) or viewer_quality.get("verdict") != "pass":
        raise RuntimeError("Gold enforcement Viewer Quality evidence missing after acceptance")
    if certificate_sha256 is None:
        raise RuntimeError("Gold enforcement packaging acceptance certificate is missing after acceptance")
    advance_stage("gold_pass")
    return plan, critic, report
