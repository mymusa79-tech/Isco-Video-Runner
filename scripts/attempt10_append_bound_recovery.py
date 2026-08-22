from __future__ import annotations

import scripts.append_retry_guard as append_guard


_MARKER = "_isco_attempt10_append_bound_recovery"


def _recoverable_first_pass_split(
    additions: dict[str, str],
    target_specs: list[dict],
    *,
    aggregate_headroom: int,
) -> tuple[dict[str, str], list[str]]:
    """Hold hard-band-safe first-pass additions and discard only bound-invalid targets.

    This hook is intentionally narrow. It is used only by append_retry_guard's existing
    aggregate-underlength path, where one target-completion provider call already exists.
    A first response that is too short, exceeds maximum_append_words, or would exceed the
    170-word section ceiling is discarded for that id and routed into that same single
    completion slot. No text is applied before the complete repair passes the original
    validators. The completion response itself is not recoverable and still fails closed.
    """
    specs_by_id = {str(spec["id"]): spec for spec in target_specs}
    held: dict[str, str] = {}
    retry_ids: list[str] = []
    recovery_reasons: list[str] = []

    for section_id, append_text in additions.items():
        spec = specs_by_id[section_id]
        words = append_guard._word_count(append_text)
        current_words = int(spec["current_words"])
        section_minimum, section_maximum = [int(v) for v in spec["hard_section_band"]]
        maximum_append_words = int(spec["maximum_append_words"])
        projected_words = current_words + words

        reason = None
        if words > maximum_append_words:
            reason = "over_max_append"
        elif projected_words > section_maximum:
            reason = "over_section_ceiling"
        elif projected_words < section_minimum:
            reason = "under_section_floor"

        if reason is not None:
            retry_ids.append(section_id)
            recovery_reasons.append(f"{section_id}:{reason}")
            continue

        append_guard._validate_addition_bounds(
            {section_id: append_text},
            [spec],
            aggregate_headroom=aggregate_headroom,
        )
        held[section_id] = append_text

    if retry_ids:
        print(
            "Attempt 10 first-pass append bound recovery: discarded="
            + ",".join(recovery_reasons)
            + " existing_target_completion_limit=1 no_partial_apply=true"
        )

    return held, retry_ids


def install_attempt10_append_bound_recovery() -> None:
    """Patch only first-pass aggregate-underlength classification; keep all hard gates."""
    current = append_guard._split_held_and_underfloor_additions
    if getattr(current, _MARKER, False):
        return

    setattr(_recoverable_first_pass_split, _MARKER, True)
    append_guard._split_held_and_underfloor_additions = _recoverable_first_pass_split

    print(
        "Attempt 10 append bound recovery installed: first-pass under/over-bound targets "
        "may consume only the existing one bounded completion call; completion and final "
        "Film 110-170 / 800-1450 gates remain strict"
    )
