from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from isco_video_agent.ai_budget import BudgetLedger
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

from scripts.gold_final_critic_text_fallback import gold_final_critic_text_fallback
from scripts.gold_shadow_phase2a import _fingerprint, _provider_attempt_total
from scripts.gold_thumbnail_budget import build_budgeted_thumbnail_package


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_gold_enforce_phase4(
    *,
    output_dir: Path,
    gemini: str,
    pexels: str,
    ledger: BudgetLedger,
    pixabay: str | None = None,
) -> tuple[object, dict, dict]:
    """Enforce the extracted Gold finalizer over the exact existing core render.

    Phase 4 deliberately does not switch the production entrypoint yet: Runner still
    calls orchestrator.produce() once, then this function performs the Gold
    thumbnail->rights->critic->state sequence on that same output. Any Gold failure is
    now authoritative and raises before manifest/analytics. No second render exists.
    """
    final_path = output_dir / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Final video missing before Gold enforcement")
    final_sha_before = _sha256_file(final_path)
    attempts_before = _provider_attempt_total(ledger)
    state_before = _fingerprint(Path(os.environ.get("ISCO_HISTORY_PATH", ""))) if os.environ.get("ISCO_HISTORY_PATH") else {
        "exists": None,
        "sha256": None,
        "observation_status": "not_configured",
    }
    critic_box: dict[str, dict] = {}

    def budgeted_builder(**kwargs):
        return build_budgeted_thumbnail_package(**kwargs, ledger=ledger, pixabay_key=pixabay)

    def enforced_critic(**kwargs):
        # Gold-only P1 recovery: keep Opening Vision on direct Gemini, while the
        # text-only release review may make exactly one technical provider switch
        # Gemini -> OpenRouter. Core final_critic.py remains unchanged.
        with gold_final_critic_text_fallback():
            critic = _run_final_critic(
                **kwargs,
                ledger=ledger,
                release_mode="enforce",
                report_dir=output_dir,
                task_prefix="GOLD_",
                task_kind="GOLD_FINAL_CRITIC",
            )
        critic_box["critic"] = critic
        # Validate the one-render invariant while still inside finalize_gold_output's
        # cleanup-protected try block. If bytes changed, the finalizer rejects/cleans
        # the production before mark_production_accepted can execute.
        if _sha256_file(final_path) != final_sha_before:
            raise RuntimeError("Gold enforcement detected final.mp4 mutation before state acceptance")
        return critic

    error: Exception | None = None
    plan = None
    critic: dict = {}
    try:
        plan, critic = finalize_gold_output(
            output_dir=output_dir,
            output_key=_output_key(output_dir),
            gemini=gemini,
            pexels=pexels,
            plan_from_json=_plan_from_json,
            build_thumbnail_package=budgeted_builder,
            augment_rights=_augment_rights,
            run_final_critic=enforced_critic,
            remove_production_record=remove_production_record,
            mark_production_accepted=mark_production_accepted,
            sync_state_snapshot=_sync_state_snapshot,
        )
    except Exception as exc:
        error = exc
        critic = critic_box.get("critic", critic)

    attempts_after = _provider_attempt_total(ledger)
    final_sha_after = _sha256_file(final_path) if final_path.is_file() else None
    state_after = _fingerprint(Path(os.environ.get("ISCO_HISTORY_PATH", ""))) if os.environ.get("ISCO_HISTORY_PATH") else {
        "exists": None,
        "sha256": None,
        "observation_status": "not_configured",
    }
    report = {
        "schema_version": 1,
        "phase": "4",
        "mode": "enforce",
        "release_authority": "gold",
        "single_render": True,
        "entrypoint_switched": False,
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
            "failure_cleanup_expected": error is not None,
        },
        "budget": {
            "same_ledger": True,
            "provider_attempts_before_gold": attempts_before,
            "provider_attempts_after_gold": attempts_after,
            "gold_provider_attempt_delta": max(0, attempts_after - attempts_before),
        },
        # Never persist raw exception text here: provider/library errors can contain
        # request URLs, headers, query strings, or credential-bearing file paths.
        "error": safe_error(error) if error is not None else None,
    }
    try:
        (output_dir / "gold-enforce-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

    if error is not None:
        raise error
    if final_sha_after != final_sha_before:
        # Defensive belt-and-suspenders guard; the pre-accept check above should have
        # caught this already. Do not let manifest/analytics proceed on divergence.
        raise RuntimeError("Gold enforcement final.mp4 invariant failed after acceptance")
    return plan, critic, report
