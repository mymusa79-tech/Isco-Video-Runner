from __future__ import annotations

import json

from scripts import producer_quality_contract


_DIRECT_IMPERATIVE_ISSUE = "moment_direct_imperative_in_story_beat"
_NARRATION_TOO_LONG_ISSUE = "moment_narration_likely_exceeds_natural_duration"


def moment_direct_imperative_targets(plan: object) -> list[str]:
    """Delegate target ownership to the authoritative Producer acceptance contract."""
    return producer_quality_contract.moment_direct_imperative_targets(plan)


def _imperative_guidance(plan: object) -> str:
    targets = moment_direct_imperative_targets(plan)
    target_payload = json.dumps(targets, ensure_ascii=False, separators=(",", ":"))
    return (
        "DETERMINISTIC_ACCEPTANCE_RULE moment_direct_imperative_in_story_beat: "
        "the Moment hook, sections[0].on_screen_text, and closing_payoff must not begin "
        "with a direct imperative matched by the Producer gate; call_to_action/cta may "
        "remain imperative. "
        f"FAILING_FIELD_PATHS={target_payload}. "
        "Repair the listed failing story fields into reflective/observational wording "
        "without moving the command into another story field. Preserve meaning and all "
        "unrelated fields except where another listed Producer issue requires a minimal change."
    )


def short_producer_repair_guidance(plan: object, issues: list[str]) -> str:
    """Compile Producer issue IDs into deterministic, field-scoped repair guidance.

    Each recognized issue contributes its own guidance block; issues with no
    deterministic guidance here still reach the repair prompt via the bare issue list
    in _repair_short_plan_once()'s issue_notes, unchanged from before this function
    supported combining more than one issue's guidance.
    """
    issue_set = set(issues)
    parts: list[str] = []
    if _DIRECT_IMPERATIVE_ISSUE in issue_set:
        parts.append(_imperative_guidance(plan))
    if _NARRATION_TOO_LONG_ISSUE in issue_set:
        template = producer_quality_contract.resolve_short_template(plan)
        duration_guidance = producer_quality_contract.short_narration_duration_guidance(plan, template)
        if duration_guidance:
            parts.append(duration_guidance)
    return "\n".join(parts)
