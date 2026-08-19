from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Callable

from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.anti_repetition import history_path
from isco_video_agent.gold_finalizer import observe_gold_output
from isco_video_agent.production_pipeline import _augment_rights

from scripts.gold_shadow_phase2a import (
    _failed_shadow_result,
    _fingerprint,
    _hard_blocks,
    _provider_attempt_total,
)
from scripts.gold_thumbnail_budget import build_budgeted_thumbnail_package


_REQUIRED_RELEASE_INPUTS = (
    "plan.json",
    "quality-final.json",
    "visual-audit.json",
    "rights-manifest.json",
    "monetization-check.json",
)


def _canonical_package_fingerprint(output_dir: Path) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for path in sorted(output_dir.glob("thumbnail-*.jpg")):
        result[path.name] = _fingerprint(path)
    plan = output_dir / "thumbnail-plan.json"
    if plan.exists():
        result[plan.name] = _fingerprint(plan)
    return result


def _stage_shadow_root(output_dir: Path) -> Path:
    """Build a private evaluation view without copying or rendering final.mp4 twice."""
    shadow_root = output_dir / "gold-shadow" / "phase2b" / "eval-root"
    if shadow_root.exists():
        shutil.rmtree(shadow_root)
    shadow_root.mkdir(parents=True, exist_ok=True)

    final = output_dir / "final.mp4"
    if not final.is_file():
        raise RuntimeError("Final video missing before Gold Thumbnail Shadow")
    # A hard link is the exact same underlying rendered bytes/inode, not a second
    # render and not a second media copy. Gold can safely create its own review media
    # next to this link without mutating the canonical V4 output package.
    os.link(final, shadow_root / "final.mp4")

    for name in _REQUIRED_RELEASE_INPUTS:
        source = output_dir / name
        if not source.is_file():
            raise RuntimeError(f"Required production report missing before Gold Thumbnail Shadow: {name}")
        shutil.copy2(source, shadow_root / name)
    return shadow_root


def _rights_mutation(before: dict, after: dict) -> bool:
    return bool(
        before.get("observation_status") == "ok"
        and after.get("observation_status") == "ok"
        and (
            before.get("exists") != after.get("exists")
            or before.get("sha256") != after.get("sha256")
        )
    )


def run_gold_shadow_phase2b(
    *,
    output_dir: Path,
    gemini: str,
    pexels: str,
    ledger: BudgetLedger,
    legacy_critic: dict,
    plan_from_json: Callable[[Path], Any],
    run_final_critic: Callable[..., dict],
) -> dict:
    """Shadow the full Gold thumbnail->rights->critic sequence on the same render.

    Canonical V4 files are fingerprinted before/after. All thumbnail files and rights
    augmentation live under gold-shadow/phase2b; history/state mutation remains
    impossible because the Engine observer API exposes no mutation callbacks.
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
    thumbnail_observation_status = "ok"
    try:
        shadow_root = _stage_shadow_root(output_dir)
        plan = plan_from_json(shadow_root / "plan.json")
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
        _plan, gold_critic, gold_result = observe_gold_output(
            output_dir=shadow_root,
            gemini=gemini,
            plan_from_json=plan_from_json,
            run_final_critic=run_final_critic,
            ledger=ledger,
            report_dir=shadow_root / "critic",
        )
    except Exception as exc:
        thumbnail_observation_status = "failed_observation"
        gold_critic, gold_result = _failed_shadow_result(exc)

    attempts_after = _provider_attempt_total(ledger)
    final_after = _fingerprint(canonical_final)
    rights_after = _fingerprint(canonical_rights)
    state_after = _fingerprint(state_path)
    package_after = _canonical_package_fingerprint(output_dir)

    final_observation_complete = (
        final_before.get("observation_status") == "ok"
        and final_after.get("observation_status") == "ok"
    )
    state_observation_complete = (
        state_before.get("observation_status") == "ok"
        and state_after.get("observation_status") == "ok"
    )
    artifact_divergence = bool(
        final_observation_complete
        and final_before.get("sha256") != final_after.get("sha256")
    )
    state_mutation_detected = bool(
        state_observation_complete
        and (
            state_before.get("exists") != state_after.get("exists")
            or state_before.get("sha256") != state_after.get("sha256")
        )
    )
    canonical_rights_mutation_detected = _rights_mutation(rights_before, rights_after)
    canonical_package_mutation_detected = package_before != package_after
    legacy_blocks = _hard_blocks(legacy_critic)
    gold_blocks = _hard_blocks(gold_critic)

    comparison = {
        "schema_version": 2,
        "phase": "2B",
        "mode": "observe_only",
        "release_authority": "legacy_v4",
        "same_render": {
            "path": str(canonical_final),
            "before": final_before,
            "after": final_after,
            "observation_complete": final_observation_complete,
            "artifact_divergence": artifact_divergence,
            "shadow_eval_path": str(shadow_root / "final.mp4") if shadow_root else None,
            "shadow_uses_hard_link": bool(
                shadow_root
                and (shadow_root / "final.mp4").exists()
                and canonical_final.exists()
                and os.stat(shadow_root / "final.mp4").st_ino == os.stat(canonical_final).st_ino
            ),
        },
        "thumbnail_shadow": {
            "observation_status": thumbnail_observation_status,
            "enabled": True,
            "canonical_thumbnail_files_before": package_before,
            "canonical_thumbnail_files_after": package_after,
            "canonical_package_mutation_detected": canonical_package_mutation_detected,
            "shadow_root": str(shadow_root) if shadow_root else None,
            "package_status": package.get("status") if isinstance(package, dict) else None,
            "candidate_count": len(package.get("candidates", [])) if isinstance(package, dict) else 0,
        },
        "rights_observation": {
            "canonical_before": rights_before,
            "canonical_after": rights_after,
            "canonical_rights_mutation_detected": canonical_rights_mutation_detected,
            "shadow_rights_path": str(shadow_root / "rights-manifest.json") if shadow_root else None,
        },
        "state_observation": {
            "path": str(state_path),
            "before": state_before,
            "after": state_after,
            "observation_complete": state_observation_complete,
            "state_mutation_detected": state_mutation_detected,
        },
        "legacy_v4": {
            "status": legacy_critic.get("status") if isinstance(legacy_critic, dict) else None,
            "observation_status": legacy_critic.get("observation_status") if isinstance(legacy_critic, dict) else None,
            "hard_blocks": legacy_blocks,
        },
        "gold_shadow": {
            "status": gold_critic.get("status") if isinstance(gold_critic, dict) else None,
            "observation_status": gold_critic.get("observation_status") if isinstance(gold_critic, dict) else None,
            "hard_blocks": gold_blocks,
            "state_semantics": gold_result.get("state_semantics", {}),
        },
        "budget": {
            "same_ledger": True,
            "provider_attempts_before_gold_shadow": attempts_before,
            "provider_attempts_after_gold_shadow": attempts_after,
            "gold_shadow_provider_attempt_delta": max(0, attempts_after - attempts_before),
            "thumbnail_tasks_are_p2": True,
        },
        "divergences": {
            "artifact_divergence": artifact_divergence,
            "canonical_rights_mutation": canonical_rights_mutation_detected,
            "canonical_thumbnail_package_mutation": canonical_package_mutation_detected,
            "deterministic_policy_divergence": legacy_blocks != gold_blocks,
            "state_semantics_divergence": state_mutation_detected,
            "verdict_divergence": (
                (legacy_critic.get("status") if isinstance(legacy_critic, dict) else None)
                != (gold_critic.get("status") if isinstance(gold_critic, dict) else None)
            ),
        },
        "gold_shadow_result": gold_result,
    }

    try:
        (output_dir / "gold-shadow-comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"Gold Shadow Phase 2B comparison write skipped ({type(exc).__name__})")

    if artifact_divergence:
        print("Gold Shadow Phase 2B WARNING: final.mp4 fingerprint changed during observation")
    if state_mutation_detected:
        print("Gold Shadow Phase 2B WARNING: history fingerprint changed during observation")
    if canonical_rights_mutation_detected or canonical_package_mutation_detected:
        print("Gold Shadow Phase 2B WARNING: canonical V4 packaging artifacts changed during shadow")
    if comparison["divergences"]["deterministic_policy_divergence"]:
        print("Gold Shadow Phase 2B divergence: deterministic hard blocks differ from Legacy V4")
    if comparison["divergences"]["verdict_divergence"]:
        print("Gold Shadow Phase 2B divergence: Legacy V4 and Gold verdicts differ")
    return comparison
