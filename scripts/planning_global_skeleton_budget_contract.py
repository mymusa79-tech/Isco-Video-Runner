from __future__ import annotations

"""Derived field budgets for the existing adaptive Global Section Skeleton owner.

This module does not own providers, retries, sharding, or quality gates.  It projects the
Engine's existing aggregate UTF-8 portability ceiling into provider-visible field budgets
and deterministic Runner validation before the adaptive outline installer binds its Core
validator.

The aggregate Engine ceiling remains authoritative and unchanged.  No Arabic text is
truncated or rewritten locally: an oversized provider output is rejected as a structural
output-contract violation and the existing Planning Stage Contract remains the sole
retry/provider owner.
"""

import functools
import json
import math
from dataclasses import replace
from typing import Any

from isco_video_agent import adaptive_outline_contract as engine_contract

from scripts import planning_outline_adaptive_sharding as adaptive
from scripts import planning_stage_contract as stage_contract


_INSTALL_MARKER = "_isco_global_skeleton_derived_budget_v1"
_PROMPT_MARKER = "<GLOBAL_SKELETON_DERIVED_BUDGET_V1>"


def _compact_utf8_bytes(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def derive_skeleton_field_budget(expected_count: int) -> dict[str, int]:
    """Derive field budgets from the Engine aggregate ceiling and JSON overhead.

    No percentage or language-specific byte constant is introduced.  First measure the
    exact compact-JSON structural cost for N empty entries.  One per-entry structural
    share becomes the maximum id budget and one additional structural share is reserved
    as aggregate slack.  The remaining bytes are divided evenly across purposes.

    A purpose may borrow that one reserved share as its hard ceiling, while the prompt
    asks every purpose to stay at the lower soft target.  The unchanged aggregate Engine
    validator remains the final authority when multiple fields simultaneously approach
    their individual hard ceilings.
    """

    if isinstance(expected_count, bool) or not isinstance(expected_count, int):
        raise ValueError("expected_count must be a positive integer")
    if expected_count <= 0:
        raise ValueError("expected_count must be a positive integer")

    aggregate_limit = int(engine_contract.GLOBAL_SECTION_SKELETON_MAX_UTF8_BYTES)
    structural_probe = [
        {"id": "", "purpose": "", "arc_position": index}
        for index in range(1, expected_count + 1)
    ]
    structural_bytes = _compact_utf8_bytes(structural_probe)
    structural_share = int(math.ceil(structural_bytes / expected_count))
    id_hard_bytes = structural_share
    aggregate_reserve_bytes = structural_share

    remaining = (
        aggregate_limit
        - structural_bytes
        - (id_hard_bytes * expected_count)
        - aggregate_reserve_bytes
    )
    if remaining <= 0:
        raise RuntimeError(
            "Global Skeleton aggregate ceiling cannot fund derived field budgets "
            f"count={expected_count} structural={structural_bytes} "
            f"limit={aggregate_limit}"
        )

    purpose_soft_bytes = remaining // expected_count
    purpose_hard_bytes = purpose_soft_bytes + aggregate_reserve_bytes
    if purpose_soft_bytes <= 0 or purpose_hard_bytes <= purpose_soft_bytes:
        raise RuntimeError("invalid derived Global Skeleton purpose budget")

    return {
        "aggregate_limit_bytes": aggregate_limit,
        "structural_bytes": structural_bytes,
        "structural_share_bytes": structural_share,
        "aggregate_reserve_bytes": aggregate_reserve_bytes,
        "id_hard_bytes": id_hard_bytes,
        "purpose_soft_bytes": purpose_soft_bytes,
        "purpose_hard_bytes": purpose_hard_bytes,
    }


def _canonical_budget_view(value: object, expected_count: int) -> list[dict] | None:
    """Return Engine-equivalent normalized entries only when shape is budget-checkable.

    Schema/semantic defects are deliberately left to the existing adaptive validator.
    This helper exists only to classify byte portability failures before the Engine turns
    the same condition into a generic adaptive contract error.
    """

    if not isinstance(value, list) or len(value) != expected_count:
        return None
    result: list[dict] = []
    required = {"id", "purpose", "arc_position"}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != required:
            return None
        section_id = str(raw.get("id") or "").strip()
        purpose = " ".join(str(raw.get("purpose") or "").strip().split())
        arc_raw = raw.get("arc_position")
        if isinstance(arc_raw, bool) or not isinstance(arc_raw, (int, float)):
            return None
        arc_position = int(arc_raw)
        if float(arc_raw) != float(arc_position):
            return None
        result.append(
            {"id": section_id, "purpose": purpose, "arc_position": arc_position}
        )
    return result


def _validate_budgeted_skeleton(
    value: object,
    contract: stage_contract.PlanningStageContract,
) -> None:
    expected_count = int(contract.semantic_rules.get("expected_count") or 0)
    if expected_count <= 0:
        return
    canonical = _canonical_budget_view(value, expected_count)
    if canonical is None:
        return

    budget = derive_skeleton_field_budget(expected_count)
    for index, item in enumerate(canonical):
        id_bytes = len(item["id"].encode("utf-8"))
        if id_bytes > budget["id_hard_bytes"]:
            stage_contract._raise_validation(
                stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
                contract,
                f"$.{engine_contract.GLOBAL_SECTION_SKELETON_FIELD}[{index}].id",
                "global_skeleton_id_portability_budget_exceeded "
                f"bytes={id_bytes} hard={budget['id_hard_bytes']} "
                f"aggregate_limit={budget['aggregate_limit_bytes']}",
            )

        purpose_bytes = len(item["purpose"].encode("utf-8"))
        if purpose_bytes > budget["purpose_hard_bytes"]:
            stage_contract._raise_validation(
                stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
                contract,
                f"$.{engine_contract.GLOBAL_SECTION_SKELETON_FIELD}[{index}].purpose",
                "global_skeleton_purpose_portability_budget_exceeded "
                f"bytes={purpose_bytes} soft={budget['purpose_soft_bytes']} "
                f"hard={budget['purpose_hard_bytes']} "
                f"aggregate_limit={budget['aggregate_limit_bytes']}",
            )

    aggregate_bytes = _compact_utf8_bytes(canonical)
    if aggregate_bytes > budget["aggregate_limit_bytes"]:
        stage_contract._raise_validation(
            stage_contract.PlanningErrorCode.STRUCTURAL_INVALID,
            contract,
            f"$.{engine_contract.GLOBAL_SECTION_SKELETON_FIELD}",
            "global_section_skeleton_portability_budget_exceeded "
            f"bytes={aggregate_bytes} limit={budget['aggregate_limit_bytes']} "
            f"purpose_soft={budget['purpose_soft_bytes']} "
            f"purpose_hard={budget['purpose_hard_bytes']}",
        )


def _install_provider_visible_budget() -> None:
    current = engine_contract.core_prompt_with_global_skeleton
    if getattr(current, _INSTALL_MARKER, False):
        return

    @functools.wraps(current)
    def budgeted_core_prompt(base_prompt: str, expected_count: int) -> str:
        prompt = current(base_prompt, expected_count)
        if _PROMPT_MARKER in prompt:
            return prompt
        budget = derive_skeleton_field_budget(expected_count)
        return (
            prompt
            + "\n"
            + _PROMPT_MARKER
            + "\nFor every section_skeleton entry: keep `id` compact (hard <= "
            + str(budget["id_hard_bytes"])
            + " UTF-8 bytes). `purpose` is ONE concise editorial function only: no "
            "explanation, examples, reasoning, or visual direction; target <= "
            + str(budget["purpose_soft_bytes"])
            + " UTF-8 bytes, hard <= "
            + str(budget["purpose_hard_bytes"])
            + " UTF-8 bytes. The unchanged whole-skeleton hard limit is "
            + str(budget["aggregate_limit_bytes"])
            + " UTF-8 bytes."
        )

    setattr(budgeted_core_prompt, _INSTALL_MARKER, True)
    engine_contract.core_prompt_with_global_skeleton = budgeted_core_prompt


def _install_core_validator_projection() -> None:
    current = adaptive._validate_core
    if getattr(current, _INSTALL_MARKER, False):
        return

    @functools.wraps(current)
    def budgeted_validate_core(
        data: dict,
        contract: stage_contract.PlanningStageContract,
    ) -> dict:
        if isinstance(data, dict):
            _validate_budgeted_skeleton(
                data.get(engine_contract.GLOBAL_SECTION_SKELETON_FIELD),
                contract,
            )
        return current(data, contract)

    setattr(budgeted_validate_core, _INSTALL_MARKER, True)
    adaptive._validate_core = budgeted_validate_core


def _install_core_spec_budget_trace() -> None:
    current = adaptive._core_stage_spec
    if getattr(current, _INSTALL_MARKER, False):
        return

    @functools.wraps(current)
    def budgeted_core_stage_spec(expected_count: int):
        spec = current(expected_count)
        budget = derive_skeleton_field_budget(expected_count)
        rules: dict[str, Any] = dict(spec.semantic_rules)
        rules.update(
            {
                "global_skeleton_id_hard_utf8_bytes": budget["id_hard_bytes"],
                "global_skeleton_purpose_soft_utf8_bytes": budget["purpose_soft_bytes"],
                "global_skeleton_purpose_hard_utf8_bytes": budget["purpose_hard_bytes"],
                "global_skeleton_budget_structural_bytes": budget["structural_bytes"],
                "global_skeleton_budget_reserve_bytes": budget[
                    "aggregate_reserve_bytes"
                ],
            }
        )
        return replace(spec, semantic_rules=rules)

    setattr(budgeted_core_stage_spec, _INSTALL_MARKER, True)
    adaptive._core_stage_spec = budgeted_core_stage_spec


def install_global_skeleton_derived_budget_contract() -> None:
    """Project the existing 1800-byte aggregate ceiling into measurable field budgets."""

    _install_provider_visible_budget()
    _install_core_validator_projection()
    _install_core_spec_budget_trace()
    sample = derive_skeleton_field_budget(8)
    print(
        "Global Skeleton derived budget contract installed: aggregate_limit="
        f"{sample['aggregate_limit_bytes']} film_purpose_soft="
        f"{sample['purpose_soft_bytes']} film_purpose_hard="
        f"{sample['purpose_hard_bytes']} film_id_hard={sample['id_hard_bytes']} "
        "utf8_bytes=true local_truncation=false retry_owner=planning_stage_contract "
        "portability_failures=STRUCTURAL_INVALID"
    )
