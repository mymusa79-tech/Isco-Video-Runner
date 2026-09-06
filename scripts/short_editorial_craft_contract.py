from __future__ import annotations

from typing import Any


# Editorial targets learned from the finished Run219 Short review. These do not
# replace Engine safety/quality gates and do not create any provider calls.
SHORT_HOOK_TARGET_SECONDS = 2.5
SHORT_HOOK_RUNTIME_MAX_SECONDS = 3.0
SHORT_HOOK_MAX_WORDS = 8
SHORT_ON_SCREEN_MAX_WORDS = 12
SHORT_PAYOFF_MAX_WORDS = 14
SHORT_CTA_MAX_WORDS = 14

# Long-form keeps its calmer pacing. The shared benefit is earlier promise clarity
# plus section atoms that can later become source-bound sibling Shorts.
LONG_OPENING_PROMISE_TARGET_SECONDS = 7.0
LONG_DERIVATIVE_ATOM_MAX_WORDS = 14


MOMENT_HOOK_ISSUE = "moment_hook_exceeds_spoken_window"
MOMENT_ON_SCREEN_DENSITY_ISSUE = "moment_on_screen_text_too_dense"
MOMENT_PAYOFF_DENSITY_ISSUE = "moment_payoff_too_dense"
MOMENT_CTA_DENSITY_ISSUE = "moment_cta_too_dense"


class ShortEditorialCraftError(RuntimeError):
    pass


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def word_count(value: object) -> int:
    return len(clean(value).split())


def compact_source_words(value: object, maximum: int) -> str:
    """Take a prefix from already-approved source text; never invent replacement words."""
    maximum = int(maximum)
    if maximum <= 0:
        raise ShortEditorialCraftError("source_compaction_word_limit_invalid")
    return " ".join(clean(value).split()[:maximum])


def craft_plan_issues(plan: object) -> list[str]:
    """Return deterministic Moment craft issues; Long remains guidance-only."""
    if clean(getattr(plan, "format", "")).lower() != "moment":
        return []

    sections = list(getattr(plan, "sections", []) or [])
    first = sections[0] if sections else None
    issues: list[str] = []

    if word_count(getattr(plan, "hook", "")) > SHORT_HOOK_MAX_WORDS:
        issues.append(MOMENT_HOOK_ISSUE)
    if first is not None and word_count(getattr(first, "on_screen_text", "")) > SHORT_ON_SCREEN_MAX_WORDS:
        issues.append(MOMENT_ON_SCREEN_DENSITY_ISSUE)
    if word_count(getattr(plan, "closing_payoff", "")) > SHORT_PAYOFF_MAX_WORDS:
        issues.append(MOMENT_PAYOFF_DENSITY_ISSUE)
    if word_count(getattr(plan, "cta", "")) > SHORT_CTA_MAX_WORDS:
        issues.append(MOMENT_CTA_DENSITY_ISSUE)
    return issues


def merge_craft_issues(base_issues: list[str], plan: object) -> list[str]:
    merged = list(base_issues)
    for issue in craft_plan_issues(plan):
        if issue not in merged:
            merged.append(issue)
    return merged


def craft_writing_directive() -> str:
    """Compact shared Producer guidance without changing any safety authority."""
    return (
        "Editorial craft: Moment hook should land in ~2.5s and must fit the <=3.0s final hook window; "
        f"keep hook <= {SHORT_HOOK_MAX_WORDS} words, on_screen_text one compact idea <= {SHORT_ON_SCREEN_MAX_WORDS} words, "
        f"payoff <= {SHORT_PAYOFF_MAX_WORDS} words, CTA <= {SHORT_CTA_MAX_WORDS} words and one concrete action. "
        "Keep the calm reflective channel voice; do not add hype or extra cuts. "
        f"Long: expose the first tension/viewer promise in the opening sentence, roughly within {LONG_OPENING_PROMISE_TARGET_SECONDS:.0f}s, "
        f"while preserving long-form breathing room; make each section key_point a derivative-ready idea <= {LONG_DERIVATIVE_ATOM_MAX_WORDS} words when feasible."
    )


def merge_craft_revision_note(existing: object) -> str:
    prior = clean(existing)
    directive = craft_writing_directive()
    if directive in prior:
        return prior
    return f"{prior} {directive}" if prior else directive


def repair_guidance(issues: list[str]) -> str:
    selected = set(issues)
    rules: list[str] = []
    if MOMENT_HOOK_ISSUE in selected:
        rules.append(
            f"hook: rewrite the same approved idea in <= {SHORT_HOOK_MAX_WORDS} words so it can land within the "
            f"~{SHORT_HOOK_TARGET_SECONDS:.1f}s target / <= {SHORT_HOOK_RUNTIME_MAX_SECONDS:.1f}s hard final beat; preserve the hook's tension or contrast"
        )
    if MOMENT_ON_SCREEN_DENSITY_ISSUE in selected:
        rules.append(
            f"sections[0].on_screen_text: keep one compact viewer-facing idea in <= {SHORT_ON_SCREEN_MAX_WORDS} words"
        )
    if MOMENT_PAYOFF_DENSITY_ISSUE in selected:
        rules.append(
            f"closing_payoff: preserve the earned meaning in <= {SHORT_PAYOFF_MAX_WORDS} words"
        )
    if MOMENT_CTA_DENSITY_ISSUE in selected:
        rules.append(
            f"cta: keep exactly one concrete action in <= {SHORT_CTA_MAX_WORDS} words"
        )
    if not rules:
        return ""
    return (
        "DETERMINISTIC_EDITORIAL_CRAFT_RULES: "
        + "; ".join(rules)
        + ". Do not change unrelated fields, safety/factual boundaries, template, or channel identity."
    )


def source_hook_score(text: str, *, template: str, signal_score: int) -> tuple[int, int, int]:
    """Stable source-only hook ranking: template evidence, compactness, then source order tie-break."""
    words = word_count(text)
    fits_window = 1 if 0 < words <= SHORT_HOOK_MAX_WORDS else 0
    return (int(signal_score), fits_window, -words)


def source_safe_hook(value: object) -> str:
    """Create a compact hook only by taking approved source words.

    Paired quotation text is never truncated because doing so could alter a quote's
    expressive meaning. Long quotes therefore stay out of the hook candidate pool.
    """
    text = clean(value)
    if not text:
        return ""
    paired_quote = any(
        opening in text and closing in text
        for opening, closing in (("«", "»"), ("“", "”"))
    ) or text.count('"') >= 2
    if paired_quote and word_count(text) > SHORT_HOOK_MAX_WORDS:
        return ""
    return compact_source_words(text, SHORT_HOOK_MAX_WORDS)
