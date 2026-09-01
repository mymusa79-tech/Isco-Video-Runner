from __future__ import annotations

from functools import wraps
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator

from scripts import short_planning_repair
from scripts.producer_quality_contract import (
    ProducerQualityContractError,
    merge_producer_revision_note,
    plan_quality_issues,
    validate_plan_for_producer_handoff,
)


# Run #164 proved that Producer Quality Contract detection could fire before the
# existing Moment RepairDossier transport had a chance to repair an otherwise usable
# one-section Short. Keep the producer gate authoritative, but let only a tightly
# bounded family of presentation/template defects use the already-certified one-call
# surgical Short repair. Safety/factuality and structural contract failures stay
# fail-closed.
_REPAIRABLE_SHORT_PRODUCER_ISSUES = frozenset(
    {
        "moment_story_beats_not_distinct",
        "moment_direct_imperative_in_story_beat",
        "moment_generic_motivation_phrase",
        "why_reframe_missing_explicit_contrast_or_reframe",
    }
)
_REPAIRABLE_SECTION_SUFFIXES = (
    "_on_screen_text_serialized_list",
    "_visual_query_empty",
)
_INSTALLED = False


class ProducerPlanningLifecycleError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _issue_is_repairable(issue: str) -> bool:
    return issue in _REPAIRABLE_SHORT_PRODUCER_ISSUES or (
        issue.startswith("section_")
        and issue.endswith(_REPAIRABLE_SECTION_SUFFIXES)
    )


def short_producer_issues_are_repairable(issues: list[str]) -> bool:
    return bool(issues) and all(_issue_is_repairable(issue) for issue in issues)


def resolve_plan_for_producer_handoff(
    plan: object,
    *,
    research_context: dict | None,
    repair_fn: Callable[[object, list[str]], object] | None = None,
) -> object:
    """Validate Producer quality and allow at most one bounded Short repair."""
    issues = plan_quality_issues(plan, research_context=research_context)
    if not issues:
        return plan

    fmt = _clean(getattr(plan, "format", "")).lower()
    if (
        fmt == "moment"
        and repair_fn is not None
        and short_producer_issues_are_repairable(issues)
    ):
        repaired = repair_fn(plan, issues)
        remaining = plan_quality_issues(
            repaired,
            research_context=research_context,
        )
        if remaining:
            print(
                "Producer Short repair exhausted: "
                f"remaining={','.join(remaining)} action=fail_closed"
            )
            raise ProducerQualityContractError(
                "producer_plan_handoff_blocked:" + ",".join(remaining)
            )
        print(
            "Producer Short repair PASS: "
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

        repair_fn = None
        if (
            _clean(getattr(plan, "format", "")).lower() == "moment"
            and short_planning_repair.active_short_repair_context() is None
        ):
            repair_fn = lambda candidate, issues: _repair_short_plan_once(
                candidate,
                issues,
                args=args,
                kwargs=updated,
                research_context=research_context,
            )

        return resolve_plan_for_producer_handoff(
            plan,
            research_context=research_context,
            repair_fn=repair_fn,
        )

    wrapped._isco_producer_quality_contract = True
    wrapped._isco_producer_quality_original = original
    wrapped._isco_producer_planning_lifecycle = True
    orchestrator.build_plan = wrapped
    _INSTALLED = True
    print(
        "Producer planning lifecycle installed: "
        "repairable Short pre-gate issues -> one surgical repair -> revalidate; "
        "all non-repairable issues fail closed"
    )
