from __future__ import annotations

import json

from scripts import producer_quality_contract


_DIRECT_IMPERATIVE_ISSUE = "moment_direct_imperative_in_story_beat"


def moment_direct_imperative_targets(plan: object) -> list[str]:
    """Return only viewer-facing story fields rejected by the authoritative Producer gate.

    The predicate intentionally comes from producer_quality_contract so repair guidance
    cannot silently drift from acceptance. CTA is intentionally absent because the gate
    allows an imperative CTA.
    """
    sections = list(getattr(plan, "sections", []) or [])
    candidates: list[tuple[str, object]] = [
        ("hook", getattr(plan, "hook", "")),
    ]
    if sections:
        candidates.append(
            ("sections[0].on_screen_text", getattr(sections[0], "on_screen_text", ""))
        )
    candidates.append(("closing_payoff", getattr(plan, "closing_payoff", "")))

    targets: list[str] = []
    for path, raw_value in candidates:
        value = producer_quality_contract._clean(raw_value)
        if value and producer_quality_contract._SHORT_IMPERATIVE_RE.search(value):
            targets.append(path)
    return targets


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
