from __future__ import annotations

import json
import re
from functools import wraps
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import (
    Capability,
    Priority,
    TaskSpec,
    budget_task_scope,
    get_active_budget_task,
)

from scripts import native_short_stage_contract
from scripts import planning_repair_identity_family
from scripts import planning_stage_contract
from scripts import run120_dossier_repair_hardening
from scripts import short_planning_repair
from scripts.producer_quality_contract import (
    ProducerQualityContractError,
    merge_producer_revision_note,
    plan_quality_issues,
    validate_plan_for_producer_handoff,
)


# Run #164 proved that Producer Quality Contract detection could fire before the
# existing Moment RepairDossier transport had a chance to repair an otherwise usable
# one-section Short. The follow-up long-form parity closure applies the same lifecycle
# principle only where the existing Long RepairDossier actually owns the affected
# fields. Safety/factuality, structural contract failures, and fields not writable by
# the selected repair transport remain fail-closed.
_REPAIRABLE_SHORT_PRODUCER_ISSUES = frozenset(
    {
        "moment_story_beats_not_distinct",
        "moment_direct_imperative_in_story_beat",
        "moment_generic_motivation_phrase",
        "why_reframe_missing_explicit_contrast_or_reframe",
    }
)
_REPAIRABLE_SHORT_SECTION_SUFFIXES = (
    "_on_screen_text_serialized_list",
    "_visual_query_empty",
)
_REPAIRABLE_LONG_PRODUCER_ISSUES = frozenset(
    {
        "long_form_duplicate_key_points",
    }
)
_INSTALLED = False


class ProducerPlanningLifecycleError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _semantic_key(value: object) -> str:
    text = _clean(value).lower()
    return " ".join(re.sub(r"[^\w\u0600-\u06ff]+", " ", text).split())


def _short_issue_is_repairable(issue: str) -> bool:
    return issue in _REPAIRABLE_SHORT_PRODUCER_ISSUES or (
        issue.startswith("section_")
        and issue.endswith(_REPAIRABLE_SHORT_SECTION_SUFFIXES)
    )


def short_producer_issues_are_repairable(issues: list[str]) -> bool:
    return bool(issues) and all(_short_issue_is_repairable(issue) for issue in issues)


def long_producer_issues_are_repairable(issues: list[str]) -> bool:
    """True only when every issue is writable by the existing Long Dossier transport."""
    return bool(issues) and all(issue in _REPAIRABLE_LONG_PRODUCER_ISSUES for issue in issues)


def _duplicate_long_key_point_target_ids(plan: object) -> list[str]:
    """Return only later duplicate sections, preserving the first semantic occurrence."""
    seen: set[str] = set()
    targets: list[str] = []
    for section in list(getattr(plan, "sections", []) or []):
        key = _semantic_key(getattr(section, "key_point", ""))
        if not key:
            continue
        if key in seen:
            section_id = _clean(getattr(section, "id", ""))
            if section_id:
                targets.append(section_id)
        else:
            seen.add(key)
    return targets


def resolve_plan_for_producer_handoff(
    plan: object,
    *,
    research_context: dict | None,
    repair_fn: Callable[[object, list[str]], object] | None = None,
    long_repair_fn: Callable[[object, list[str]], object] | None = None,
) -> object:
    """Validate Producer quality and allow at most one format-capable bounded repair."""
    issues = plan_quality_issues(plan, research_context=research_context)
    if not issues:
        return plan

    fmt = _clean(getattr(plan, "format", "")).lower()
    selected_repair: Callable[[object, list[str]], object] | None = None
    label = ""

    if (
        fmt == "moment"
        and repair_fn is not None
        and short_producer_issues_are_repairable(issues)
    ):
        selected_repair = repair_fn
        label = "Short"
    elif (
        fmt in {"film", "story"}
        and long_repair_fn is not None
        and long_producer_issues_are_repairable(issues)
    ):
        selected_repair = long_repair_fn
        label = "Long"

    if selected_repair is not None:
        repaired = selected_repair(plan, issues)
        remaining = plan_quality_issues(
            repaired,
            research_context=research_context,
        )
        if remaining:
            print(
                f"Producer {label} repair exhausted: "
                f"remaining={','.join(remaining)} action=fail_closed"
            )
            raise ProducerQualityContractError(
                "producer_plan_handoff_blocked:" + ",".join(remaining)
            )
        print(
            f"Producer {label} repair PASS: "
            f"repaired={','.join(issues)} repair_calls=1"
        )
        return repaired

    return validate_plan_for_producer_handoff(
        plan,
        research_context=research_context,
    )


def _planner_arg(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    index: int,
    name: str,
) -> Any:
    if len(args) > index:
        return args[index]
    return kwargs.get(name)


def _preserve_short_metadata(source: object, repaired: object) -> None:
    intent = getattr(source, "editorial_intent", None)
    if isinstance(intent, dict):
        setattr(repaired, "editorial_intent", dict(intent))
    narrative = getattr(source, "narrative_format", None)
    if narrative:
        setattr(repaired, "narrative_format", narrative)


def _repair_short_plan_once(
    plan: object,
    issues: list[str],
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    research_context: dict | None,
) -> object:
    api_key = _planner_arg(args, kwargs, 0, "api_key")
    topic = _planner_arg(args, kwargs, 1, "topic")
    requested_format = _planner_arg(args, kwargs, 2, "requested_format")
    content_model = _planner_arg(args, kwargs, 3, "content_model")

    if _clean(requested_format).lower() != "moment":
        raise ProducerPlanningLifecycleError(
            "producer_short_repair_requires_moment_request"
        )
    if _clean(getattr(plan, "format", "")).lower() != "moment":
        raise ProducerPlanningLifecycleError(
            "producer_short_repair_requires_moment_plan"
        )

    issue_notes = (
        "Producer pre-gate repairable Short issues:\n"
        + "\n".join(f"- {issue}" for issue in issues)
    )
    print(
        "Producer Short repair: "
        f"issues={','.join(issues)} mode=existing_surgical_transport repair_calls=1"
    )

    # Run #193: Producer repair runs after the native Moment Draft/Review build has
    # returned, so it is outside native_short_stage_contract's lifecycle call-state.
    # Run #197 then proved that this capability boundary must also own immutable repair
    # identity independent of process-global installer order. Resolve the authoritative
    # v2 repair StageSpec directly from the identity family; the helper is idempotent
    # when the fully composed runtime has already wrapped moment_stage_spec.
    repair_stage = planning_repair_identity_family.short_repair_stage_spec(
        str(topic or ""),
    )
    with planning_stage_contract.request_stage_scope(repair_stage):
        repaired = short_planning_repair._repair_existing_moment(
            plan,
            issue_notes,
            api_key=api_key,
            topic=str(topic or ""),
            requested_format="moment",
            content_model=str(content_model or ""),
            research_context=research_context,
        )
    _preserve_short_metadata(plan, repaired)
    return repaired


def _active_long_dossier_repair_context() -> object | None:
    """Read Run120's canonical repair ContextVar without creating a second owner."""
    context_var = getattr(run120_dossier_repair_hardening, "_REPAIR_CONTEXT", None)
    getter = getattr(context_var, "get", None)
    if not callable(getter):
        return None
    return getter()


def _producer_long_repair_spec(target_count: int) -> TaskSpec:
    # One Producer lifecycle repair may internally shard the targeted sections and the
    # existing provider router may need fallback attempts. Bound this logical P1 task
    # to the same worst-case 3 attempts per single-section target while leaving the
    # run-wide hard cap and P1 reserve fully authoritative.
    bounded_targets = max(1, int(target_count))
    return TaskSpec(
        task_id="PRODUCER_LONG_REPAIR_R1",
        kind="OUTLINE_PLAN",
        priority=Priority.P1,
        capability=Capability.TEXT,
        max_provider_attempts=3 * bounded_targets,
        schema_repair_allowed=True,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _repair_long_plan_once(
    plan: object,
    issues: list[str],
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    research_context: dict | None,
) -> object:
    api_key = _planner_arg(args, kwargs, 0, "api_key")
    topic = _planner_arg(args, kwargs, 1, "topic")
    requested_format = _clean(_planner_arg(args, kwargs, 2, "requested_format")).lower()
    content_model = str(_planner_arg(args, kwargs, 3, "content_model") or "")

    plan_format = _clean(getattr(plan, "format", "")).lower()
    if requested_format not in {"film", "story"}:
        raise ProducerPlanningLifecycleError(
            "producer_long_repair_requires_long_request"
        )
    if plan_format != requested_format:
        raise ProducerPlanningLifecycleError(
            "producer_long_repair_request_plan_format_mismatch"
        )
    if not long_producer_issues_are_repairable(issues):
        raise ProducerPlanningLifecycleError(
            "producer_long_repair_received_unsupported_issue_family"
        )

    target_ids = _duplicate_long_key_point_target_ids(plan)
    if not target_ids:
        raise ProducerPlanningLifecycleError(
            "producer_long_repair_resolved_no_duplicate_targets"
        )

    issue_notes = (
        "Producer pre-gate repairable Long issues:\n"
        + "\n".join(f"- {issue}" for issue in issues)
        + "\nTARGET_SECTION_IDS="
        + json.dumps(target_ids, ensure_ascii=False, separators=(",", ":"))
        + "\nPreserve the first occurrence of each key point. Rewrite only the targeted "
        "section narration/key_point enough to give it a genuinely distinct useful role; "
        "do not change the episode thesis, section order, factual boundaries, CTA, payoff, "
        "visual plan, or host-managed identity."
    )
    print(
        "Producer Long repair: "
        f"issues={','.join(issues)} targets={','.join(target_ids)} "
        "mode=existing_bounded_dossier_transport repair_calls=1"
    )

    def execute() -> object:
        return run120_dossier_repair_hardening._repair_existing_plan(
            plan,
            issue_notes,
            api_key=api_key,
            topic=str(topic or ""),
            requested_format=requested_format,
            content_model=content_model,
            research_context=research_context,
        )

    active = get_active_budget_task()
    if active is None:
        # Unit/Engine-only compatibility. Canonical Runner production always invokes
        # planning inside a routed BudgetLedger task; no synthetic ledger is invented.
        return execute()

    spec = _producer_long_repair_spec(len(target_ids))
    requested_model = content_model or str(active.requested_model or "")
    with budget_task_scope(
        active.ledger,
        spec,
        requested_model=requested_model,
    ):
        return execute()


def install_producer_planning_lifecycle() -> None:
    """Replace the detector-only Producer wrapper with lifecycle-aware ownership."""
    global _INSTALLED
    current = orchestrator.build_plan
    if getattr(current, "_isco_producer_planning_lifecycle", False):
        _INSTALLED = True
        return
    if not getattr(current, "_isco_producer_quality_contract", False):
        raise ProducerPlanningLifecycleError(
            "producer_quality_contract_must_be_installed_first"
        )

    original = getattr(current, "_isco_producer_quality_original", None)
    if not callable(original):
        raise ProducerPlanningLifecycleError(
            "producer_quality_original_planner_missing"
        )

    @wraps(current)
    def wrapped(*args, **kwargs):
        research_context = kwargs.get("research_context")
        updated = dict(kwargs)
        updated["revision_note"] = merge_producer_revision_note(
            updated.get("revision_note", ""),
            research_context,
        )
        plan = original(*args, **updated)

        fmt = _clean(getattr(plan, "format", "")).lower()
        short_repair_fn = None
        long_repair_fn = None

        if (
            fmt == "moment"
            and short_planning_repair.active_short_repair_context() is None
        ):
            short_repair_fn = lambda candidate, issues: _repair_short_plan_once(
                candidate,
                issues,
                args=args,
                kwargs=updated,
                research_context=research_context,
            )
        elif (
            fmt in {"film", "story"}
            and _active_long_dossier_repair_context() is None
        ):
            long_repair_fn = lambda candidate, issues: _repair_long_plan_once(
                candidate,
                issues,
                args=args,
                kwargs=updated,
                research_context=research_context,
            )

        return resolve_plan_for_producer_handoff(
            plan,
            research_context=research_context,
            repair_fn=short_repair_fn,
            long_repair_fn=long_repair_fn,
        )

    wrapped._isco_producer_quality_contract = True
    wrapped._isco_producer_quality_original = original
    wrapped._isco_producer_planning_lifecycle = True
    orchestrator.build_plan = wrapped
    _INSTALLED = True
    print(
        "Producer planning lifecycle installed: "
        "repairable Short/Long pre-gate issues -> one capability-owned repair -> "
        "revalidate; unsupported issue families fail closed"
    )
