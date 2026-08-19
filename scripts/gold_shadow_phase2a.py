from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.anti_repetition import history_path
from isco_video_agent.gold_finalizer import observe_gold_output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> dict:
    try:
        if not path.exists():
            return {"exists": False, "sha256": None, "observation_status": "ok"}
        return {"exists": True, "sha256": _sha256_file(path), "observation_status": "ok"}
    except Exception as exc:
        return {
            "exists": None,
            "sha256": None,
            "observation_status": "failed_observation",
            "error_type": type(exc).__name__,
        }


def _provider_attempt_total(ledger: BudgetLedger) -> int:
    try:
        return int(ledger.to_summary().get("provider_attempts", {}).get("total", 0) or 0)
    except Exception:
        return 0


def _hard_blocks(critic: dict) -> list[str]:
    raw = critic.get("hard_blocks", []) if isinstance(critic, dict) else []
    return sorted(str(item) for item in raw if str(item).strip()) if isinstance(raw, list) else []


def _failed_shadow_result(exc: Exception) -> tuple[dict, dict]:
    critic = {
        "status": "block",
        "mode": "observe_only",
        "observation_status": "failed_observation",
        "would_block_if_enforced": True,
        "hard_blocks": [],
        "model_review": {
            "status": "block",
            "critical_issues": ["Gold shadow observer failed outside the Engine safety boundary"],
            "improvements": [],
            "summary": f"{type(exc).__name__}: runner shadow wrapper failed",
        },
    }
    result = {
        "schema_version": 1,
        "phase": "2A",
        "mode": "observe_only",
        "release_authority": "legacy_v4",
        "observation_status": "failed_observation",
        "critic": critic,
        "state_semantics": {
            "would_accept": False,
            "would_reject": True,
            "would_mark_production_accepted": False,
            "would_remove_history_record": True,
            "would_sync_state_snapshot": True,
            "state_mutation_performed": False,
            "thumbnail_enabled": False,
            "rights_augmentation_enabled": False,
            "postpublish_learning_enabled": False,
        },
    }
    return critic, result


def run_gold_shadow_phase2a(
    *,
    output_dir: Path,
    gemini: str,
    ledger: BudgetLedger,
    legacy_critic: dict,
    plan_from_json: Callable[[Path], Any],
    run_final_critic: Callable[..., dict],
) -> dict:
    """Evaluate the exact V4 render with Gold critic/state semantics, observe-only.

    This wrapper is deliberately fail-open and cannot change release authority. It
    fingerprints final.mp4 and history before/after the Gold observer, records the
    same-ledger provider-attempt delta, and writes a comparison sidecar. Any observer
    exception becomes evidence rather than a production failure.
    """
    final_path = output_dir / "final.mp4"
    state_path = history_path()
    final_before = _fingerprint(final_path)
    state_before = _fingerprint(state_path)
    attempts_before = _provider_attempt_total(ledger)
    report_dir = output_dir / "gold-shadow" / "phase2a"

    try:
        _plan, gold_critic, gold_result = observe_gold_output(
            output_dir=output_dir,
            gemini=gemini,
            plan_from_json=plan_from_json,
            run_final_critic=run_final_critic,
            ledger=ledger,
            report_dir=report_dir,
        )
    except Exception as exc:
        # Belt-and-suspenders containment around the Engine's already fail-open
        # observer: Phase 2A must never create a new V4 release failure mode.
        gold_critic, gold_result = _failed_shadow_result(exc)

    attempts_after = _provider_attempt_total(ledger)
    final_after = _fingerprint(final_path)
    state_after = _fingerprint(state_path)

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
    legacy_blocks = _hard_blocks(legacy_critic)
    gold_blocks = _hard_blocks(gold_critic)

    comparison = {
        "schema_version": 1,
        "phase": "2A",
        "mode": "observe_only",
        "release_authority": "legacy_v4",
        "same_render": {
            "path": str(final_path),
            "before": final_before,
            "after": final_after,
            "observation_complete": final_observation_complete,
            "artifact_divergence": artifact_divergence,
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
        },
        "divergences": {
            "artifact_divergence": artifact_divergence,
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
        print(f"Gold Shadow Phase 2A comparison write skipped ({type(exc).__name__})")

    if artifact_divergence:
        print("Gold Shadow Phase 2A WARNING: final.mp4 fingerprint changed during observation")
    if state_mutation_detected:
        print("Gold Shadow Phase 2A WARNING: history fingerprint changed during observation")
    if comparison["divergences"]["deterministic_policy_divergence"]:
        print("Gold Shadow Phase 2A divergence: deterministic hard blocks differ from Legacy V4")
    if comparison["divergences"]["verdict_divergence"]:
        print("Gold Shadow Phase 2A divergence: Legacy V4 and Gold verdicts differ")
    return comparison
