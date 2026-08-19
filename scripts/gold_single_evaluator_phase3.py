from __future__ import annotations

import json
import os
from pathlib import Path

from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.anti_repetition import history_path
from isco_video_agent.gold_finalizer import _shadow_state_semantics
from isco_video_agent.production_pipeline import _augment_rights, _plan_from_json, _run_final_critic

from scripts.gold_shadow_phase2a import _fingerprint, _hard_blocks, _provider_attempt_total
from scripts.gold_shadow_phase2b import _canonical_package_fingerprint, _stage_shadow_root, _rights_mutation
from scripts.gold_thumbnail_budget import build_budgeted_thumbnail_package


def run_gold_single_evaluator_phase3(
    *,
    output_dir: Path,
    gemini: str,
    pexels: str,
    ledger: BudgetLedger,
) -> tuple[dict, dict]:
    """Run exactly one Gold evaluator while legacy V4 retains release authority.

    The existing core render is reused. Thumbnail packaging and rights augmentation stay
    inside the Phase-2B shadow root, but the single Gold critic now owns the canonical
    observer reports (final-critic.json/opening-visual-audit.json). Its verdict remains
    observe-only and cannot change the V4 workflow exit/release decision in Phase 3.
    """
    canonical_final = output_dir / "final.mp4"
    canonical_rights = output_dir / "rights-manifest.json"
    state_path = history_path()
    final_before = _fingerprint(canonical_final)
    rights_before = _fingerprint(canonical_rights)
    state_before = _fingerprint(state_path)
    package_before = _canonical_package_fingerprint(output_dir)
    attempts_before = _provider_attempt_total(ledger)

    shadow_root: Path | None = None
    package: dict | None = None
    observation_status = "ok"
    try:
        shadow_root = _stage_shadow_root(output_dir)
        plan = _plan_from_json(shadow_root / "plan.json")
        content_model = os.environ.get("GEMINI_CONTENT_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
        package = build_budgeted_thumbnail_package(
            gemini_key=gemini,
            pexels_key=pexels,
            plan=plan,
            output_dir=shadow_root,
            model=content_model,
            ledger=ledger,
        )
        (shadow_root / "thumbnail-plan.json").write_text(
            json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _augment_rights(shadow_root, package)
        critic = _run_final_critic(
            output_dir=shadow_root,
            plan=plan,
            gemini=gemini,
            content_model=content_model,
            ledger=ledger,
            release_mode="observe_only",
            report_dir=output_dir,
            task_prefix="GOLD_",
            task_kind="GOLD_FINAL_CRITIC",
        )
    except Exception as exc:
        observation_status = "failed_observation"
        critic = {
            "status": "block",
            "mode": "observe_only",
            "observation_status": "failed_observation",
            "would_block_if_enforced": True,
            "hard_blocks": [],
            "model_review": {
                "status": "block",
                "critical_issues": ["Single Gold evaluator could not complete safely"],
                "improvements": [],
                "summary": f"{type(exc).__name__}: Phase 3 observer failed",
            },
            "release_rule": "Phase 3 keeps Legacy V4 release authority; Gold remains observe-only.",
        }
        try:
            (output_dir / "final-critic.json").write_text(
                json.dumps(critic, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    state_semantics = _shadow_state_semantics(critic)
    attempts_after = _provider_attempt_total(ledger)
    final_after = _fingerprint(canonical_final)
    rights_after = _fingerprint(canonical_rights)
    state_after = _fingerprint(state_path)
    package_after = _canonical_package_fingerprint(output_dir)

    artifact_divergence = bool(
        final_before.get("observation_status") == "ok"
        and final_after.get("observation_status") == "ok"
        and final_before.get("sha256") != final_after.get("sha256")
    )
    state_mutation_detected = bool(
        state_before.get("observation_status") == "ok"
        and state_after.get("observation_status") == "ok"
        and (
            state_before.get("exists") != state_after.get("exists")
            or state_before.get("sha256") != state_after.get("sha256")
        )
    )
    canonical_rights_mutation_detected = _rights_mutation(rights_before, rights_after)
    canonical_package_mutation_detected = package_before != package_after

    result = {
        "schema_version": 3,
        "phase": "3",
        "mode": "observe_only",
        "release_authority": "legacy_v4",
        "single_gold_evaluator": True,
        "legacy_v4": {
            "evaluator_run": False,
            "release_gate_unchanged": True,
        },
        "gold": {
            "status": critic.get("status"),
            "observation_status": critic.get("observation_status", observation_status),
            "hard_blocks": _hard_blocks(critic),
            "state_semantics": state_semantics,
            "task_namespace": "GOLD_FINAL_CRITIC",
        },
        "thumbnail_shadow": {
            "enabled": True,
            "package_status": package.get("status") if isinstance(package, dict) else None,
            "candidate_count": len(package.get("candidates", [])) if isinstance(package, dict) else 0,
            "shadow_root": str(shadow_root) if shadow_root else None,
            "canonical_package_mutation_detected": canonical_package_mutation_detected,
        },
        "same_render": {
            "before": final_before,
            "after": final_after,
            "artifact_divergence": artifact_divergence,
            "shadow_uses_hard_link": bool(
                shadow_root
                and (shadow_root / "final.mp4").exists()
                and canonical_final.exists()
                and os.stat(shadow_root / "final.mp4").st_ino == os.stat(canonical_final).st_ino
            ),
        },
        "rights_observation": {
            "canonical_before": rights_before,
            "canonical_after": rights_after,
            "canonical_rights_mutation_detected": canonical_rights_mutation_detected,
        },
        "state_observation": {
            "before": state_before,
            "after": state_after,
            "state_mutation_detected": state_mutation_detected,
        },
        "budget": {
            "same_ledger": True,
            "provider_attempts_before_gold": attempts_before,
            "provider_attempts_after_gold": attempts_after,
            "gold_provider_attempt_delta": max(0, attempts_after - attempts_before),
        },
        "divergences": {
            "artifact_divergence": artifact_divergence,
            "canonical_rights_mutation": canonical_rights_mutation_detected,
            "canonical_thumbnail_package_mutation": canonical_package_mutation_detected,
            "state_semantics_divergence": state_mutation_detected,
        },
    }
    try:
        (output_dir / "gold-shadow-comparison.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"Gold Phase 3 report write skipped ({type(exc).__name__})")

    if artifact_divergence or canonical_rights_mutation_detected or canonical_package_mutation_detected or state_mutation_detected:
        print("Gold Phase 3 WARNING: observer isolation invariant diverged")
    return critic, result
