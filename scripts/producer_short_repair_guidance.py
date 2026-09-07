from __future__ import annotations

import json

from scripts import producer_quality_contract


_DIRECT_IMPERATIVE_ISSUE = "moment_direct_imperative_in_story_beat"


def moment_direct_imperative_targets(plan: object) -> list[str]:
    """Delegate target ownership to the authoritative Producer acceptance contract."""
    return producer_quality_contract.moment_direct_imperative_targets(plan)


def short_producer_repair_guidance(plan: object, issues: list[str]) -> str:
    """Compile Producer issue IDs into deterministic, field-scoped repair guidance."""
    if _DIRECT_IMPERATIVE_ISSUE not in set(issues):
        return ""

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
