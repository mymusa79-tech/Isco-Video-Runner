from __future__ import annotations

"""Fail-closed seal for the historical prompt-inferred Planning router.

The canonical Planning Stage Contract owns schema selection and durable checkpoint
reads/writes.  The old task-level router remains as a provider-helper module for
compatibility, but after canonical composition it must not regain cache authority.
"""

from scripts import planning_stage_contract as stage_contract
from scripts import task_level_planner_router as legacy_router


_GUARD_MARKER = "_isco_legacy_planning_authority_blocked"


def _blocked_legacy_checkpoint_authority(*_args, **_kwargs):
    raise stage_contract.PlanningStageError(
        stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        "legacy Planning checkpoint authority is disabled; use planning_stage_contract",
    )


setattr(_blocked_legacy_checkpoint_authority, _GUARD_MARKER, True)


def install_legacy_planning_authority_guard() -> None:
    """Seal dormant prompt/cache authority after the explicit router is installed."""
    stage_contract.assert_planning_stage_contract_installed()

    # The historical schema resolver may still be called by low-level provider helpers,
    # but only as this explicit-contract adapter. Prompt text itself has zero authority.
    if legacy_router._structured_schema_for_prompt is not stage_contract._explicit_schema_adapter:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "legacy prompt-inferred schema resolver regained authority",
        )

    # planning_stage_contract has its own strict versioned reader/writer. Blocking these
    # historical helpers makes accidental legacy-router reinstallation fail before it
    # can read or write a prompt-hash checkpoint.
    legacy_router._load_checkpoint = _blocked_legacy_checkpoint_authority
    legacy_router._save_checkpoint = _blocked_legacy_checkpoint_authority


def assert_legacy_planning_authority_sealed() -> None:
    stage_contract.assert_planning_stage_contract_installed()
    if legacy_router._structured_schema_for_prompt is not stage_contract._explicit_schema_adapter:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "legacy prompt-inferred schema resolver is active",
        )
    for name in ("_load_checkpoint", "_save_checkpoint"):
        if not getattr(getattr(legacy_router, name), _GUARD_MARKER, False):
            raise stage_contract.PlanningStageError(
                stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"legacy checkpoint authority is not sealed:{name}",
            )
