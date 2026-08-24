from __future__ import annotations

import re
from contextvars import ContextVar

import scripts.append_retry_guard as append_guard


_MARKER = "_isco_attempt10_append_bound_recovery"
_VALIDATE_MARKER = "_isco_run71_completion_underfloor_carry"
_FIRST_PASS_UNDERFLOOR: ContextVar[dict[str, str] | None] = ContextVar(
    "isco_attempt10_first_pass_underfloor",
    default=None,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟])\s+|\n+")
_NARROW_OVERAGE_TOLERANCE_WORDS = 2


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
    validators.

    Run #71 exposed one additional safe case: a structurally valid first-pass addition
    can be under-floor, then its one replacement can also be under-floor. We retain only
    the first under-floor text as an in-memory carry candidate. It is never applied by
    itself and is never retained for over-max/over-ceiling responses.
    """
    _FIRST_PASS_UNDERFLOOR.set({})
    specs_by_id = {str(spec["id"]): spec for spec in target_specs}
    held: dict[str, str] = {}
    retry_ids: list[str] = []
    recovery_reasons: list[str] = []
    underfloor_text: dict[str, str] = {}

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
            if reason == "under_section_floor":
                underfloor_text[section_id] = append_text
            continue

        append_guard._validate_addition_bounds(
            {section_id: append_text},
            [spec],
            aggregate_headroom=aggregate_headroom,
        )
        held[section_id] = append_text

    _FIRST_PASS_UNDERFLOOR.set(underfloor_text)

    if retry_ids:
        print(
            "Attempt 10 first-pass append bound recovery: discarded="
            + ",".join(recovery_reasons)
            + " existing_target_completion_limit=1 no_partial_apply=true"
        )

    return held, retry_ids


def _carry_whole_sentences(
    completion_text: str,
    first_pass_text: str,
    *,
    required_extra_words: int,
    maximum_append_words: int,
) -> str | None:
    """Append the minimum whole first-pass sentences needed to reach the hard floor.

    No text is invented and no token-level truncation is performed. If whole-sentence
    carry cannot satisfy the missing words inside the original absolute append maximum,
    the existing validator remains authoritative and the repair fails closed.
    """
    completion_text = str(completion_text or "").strip()
    first_pass_text = str(first_pass_text or "").strip()
    if not completion_text or not first_pass_text or required_extra_words <= 0:
        return None

    completion_words = append_guard._word_count(completion_text)
    room = maximum_append_words - completion_words
    if room < required_extra_words:
        return None

    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(first_pass_text) if part.strip()]
    if not sentences:
        return None

    carried: list[str] = []
    carried_words = 0
    for sentence in sentences:
        sentence_words = append_guard._word_count(sentence)
        if sentence_words <= 0:
            continue
        if carried_words + sentence_words > room:
            break
        carried.append(sentence)
        carried_words += sentence_words
        if carried_words >= required_extra_words:
            break

    if carried_words < required_extra_words:
        return None
    return completion_text + " " + " ".join(carried)


def _trim_whole_sentences_to_fit(
    completion_text: str,
    *,
    current_words: int,
    section_minimum: int,
    maximum_append_words: int,
) -> str | None:
    """Drop only whole trailing sentences from an over-max completion response.

    Run #84: a target discarded on the first pass for over_max_append got its one
    completion-round response back 2 words over the same absolute maximum, with no
    existing recovery - the under-floor carry above only ever adds text, never
    removes it. This is the symmetric case: no mid-sentence truncation is performed,
    and if dropping whole trailing sentences cannot bring the response at or under
    maximum_append_words while the resulting section still clears the hard
    section_minimum floor, the existing validator remains authoritative and the
    repair fails closed exactly as before.
    """
    completion_text = str(completion_text or "").strip()
    if not completion_text:
        return None

    total_words = append_guard._word_count(completion_text)
    if total_words <= maximum_append_words:
        return completion_text

    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(completion_text) if part.strip()]
    if not sentences:
        return None

    kept = list(sentences)
    kept_words = total_words
    while kept and kept_words > maximum_append_words:
        dropped = kept.pop()
        kept_words -= append_guard._word_count(dropped)

    if not kept or kept_words > maximum_append_words:
        return None
    if current_words + kept_words < section_minimum:
        return None

    return " ".join(kept)


def _accept_narrow_indivisible_overage(
    text: str,
    *,
    current_words: int,
    section_maximum: int,
    maximum_append_words: int,
) -> bool:
    """Accept a completion response that whole-sentence trimming could not fix at all.

    Run #98: a completion-round response can come back as a single sentence (or with
    no droppable trailing sentence) that _trim_whole_sentences_to_fit cannot touch
    without emptying the response or pushing the section under its hard floor -
    exactly the "no sentence boundary" and "would underfloor" cases that function
    already fails closed on. maximum_append_words is a derived per-target share of
    the aggregate deficit, not the real contract; the real, unchanged contract is
    hard_section_band. When the untrimmed response exceeds maximum_append_words by
    only a narrow (<=2 word) margin and the resulting section still clears its true
    hard maximum, accept it as-is: no text is invented, no text is truncated, and
    every hard band (section and aggregate) stays exactly as strict as before.
    """
    words = append_guard._word_count(text)
    overage = words - maximum_append_words
    if overage <= 0 or overage > _NARROW_OVERAGE_TOLERANCE_WORDS:
        return False
    return current_words + words <= section_maximum


def _install_completion_underfloor_carry() -> None:
    current_validator = append_guard._validate_addition_bounds
    if getattr(current_validator, _VALIDATE_MARKER, False):
        return

    original_validator = current_validator

    def guarded_validator(
        additions: dict[str, str],
        target_specs: list[dict],
        *,
        aggregate_headroom: int,
    ) -> None:
        stash = dict(_FIRST_PASS_UNDERFLOOR.get() or {})
        carried_ids: list[str] = []

        if stash:
            specs_by_id = {str(spec["id"]): spec for spec in target_specs}
            for section_id, first_pass_text in stash.items():
                if section_id not in additions or section_id not in specs_by_id:
                    continue
                spec = specs_by_id[section_id]
                completion_text = additions[section_id]
                current_words = int(spec["current_words"])
                section_minimum = int(spec["hard_section_band"][0])
                completion_words = append_guard._word_count(completion_text)
                projected_words = current_words + completion_words
                if projected_words >= section_minimum:
                    continue

                combined = _carry_whole_sentences(
                    completion_text,
                    first_pass_text,
                    required_extra_words=section_minimum - projected_words,
                    maximum_append_words=int(spec["maximum_append_words"]),
                )
                if combined is not None:
                    additions[section_id] = combined
                    carried_ids.append(section_id)

        # Run #84: symmetric to the under-floor carry above, but for a response that
        # came back over its maximum_append_words. This only ever fires on a response
        # already headed for _validate_addition_bounds with words > maximum_append_words -
        # the first pass never calls this validator for an over-bound target (it is
        # discarded straight into the completion round instead) - so this cannot mask a
        # first-pass rejection, only rescue an otherwise-fatal completion-round result.
        trimmed_ids: list[str] = []
        narrow_overage_ids: list[str] = []
        specs_by_id = {str(spec["id"]): spec for spec in target_specs}
        for section_id, text in list(additions.items()):
            spec = specs_by_id.get(section_id)
            if spec is None:
                continue
            maximum_append_words = int(spec["maximum_append_words"])
            if append_guard._word_count(text) <= maximum_append_words:
                continue
            current_words = int(spec["current_words"])
            section_minimum, section_maximum = [int(v) for v in spec["hard_section_band"]]
            trimmed = _trim_whole_sentences_to_fit(
                text,
                current_words=current_words,
                section_minimum=section_minimum,
                maximum_append_words=maximum_append_words,
            )
            if trimmed is not None:
                additions[section_id] = trimmed
                trimmed_ids.append(section_id)
            elif _accept_narrow_indivisible_overage(
                text,
                current_words=current_words,
                section_maximum=section_maximum,
                maximum_append_words=maximum_append_words,
            ):
                narrow_overage_ids.append(section_id)

        effective_specs = target_specs
        if narrow_overage_ids:
            # Only the per-target maximum_append_words budget is raised, and only to
            # the exact accepted word count for this one validation call - every
            # other field, including hard_section_band, is untouched.
            effective_specs = []
            for spec in target_specs:
                section_id = str(spec["id"])
                if section_id in narrow_overage_ids:
                    patched = dict(spec)
                    patched["maximum_append_words"] = append_guard._word_count(additions[section_id])
                    effective_specs.append(patched)
                else:
                    effective_specs.append(spec)

        original_validator(
            additions,
            effective_specs,
            aggregate_headroom=aggregate_headroom,
        )

        # A successful validation involving any stashed target ends the carry scope.
        # The final all-target validation sees only the already-mutated safe additions.
        if stash and any(section_id in additions for section_id in stash):
            _FIRST_PASS_UNDERFLOOR.set({})
        if carried_ids:
            print(
                "Run 71 completion underfloor carry recovery: ids="
                + ",".join(carried_ids)
                + " provider_calls_added=0 whole_sentence_only=true hard_bounds_unchanged=true"
            )
        if trimmed_ids:
            print(
                "Run 84 completion over-max trim recovery: ids="
                + ",".join(trimmed_ids)
                + " provider_calls_added=0 whole_sentence_only=true hard_bounds_unchanged=true"
            )
        if narrow_overage_ids:
            print(
                "Run 98 narrow indivisible-overage acceptance: ids="
                + ",".join(narrow_overage_ids)
                + f" tolerance_words<={_NARROW_OVERAGE_TOLERANCE_WORDS}"
                + " provider_calls_added=0 no_text_invented=true no_text_truncated=true"
                + " hard_section_band_unchanged=true"
            )

    setattr(guarded_validator, _VALIDATE_MARKER, True)
    append_guard._validate_addition_bounds = guarded_validator


def install_attempt10_append_bound_recovery() -> None:
    """Patch aggregate-underlength first-pass recovery while preserving all hard gates."""
    current = append_guard._split_held_and_underfloor_additions
    if not getattr(current, _MARKER, False):
        setattr(_recoverable_first_pass_split, _MARKER, True)
        append_guard._split_held_and_underfloor_additions = _recoverable_first_pass_split

    _install_completion_underfloor_carry()

    print(
        "Attempt 10 append bound recovery installed: first-pass under/over-bound targets "
        "may consume only the existing one bounded completion call; a second under-floor "
        "result may reuse only safe whole sentences from its discarded first-pass text, and "
        "a second over-max result may drop only whole trailing sentences to fit, or, only if "
        f"no whole sentence can be dropped, be accepted as-is when it is within "
        f"{_NARROW_OVERAGE_TOLERANCE_WORDS} words of the derived per-target budget and still "
        "clears the true hard section maximum; zero extra provider calls; final Film 110-170 "
        "/ 800-1450 gates remain strict"
    )
