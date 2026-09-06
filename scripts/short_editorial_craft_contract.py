from __future__ import annotations

from typing import Any


# Run219 editorial closure. These are zero-provider-call craft targets: they shape the
# existing Producer request and the local post-voice visual timeline only.
_TEMPLATE_SPECS: dict[str, dict[str, Any]] = {
    "why_reframe": {
        "hook_max_words": 8,
        "hook_beat_max_seconds": 3.0,
        "opening_job": "state the mistaken assumption or contrast immediately; do not spend the hook defining the topic",
    },
    "inner_dialogue": {
        "hook_max_words": 9,
        "hook_beat_max_seconds": 3.2,
        "opening_job": "open on one recognisable inner thought/question, then leave room for the turn",
    },
    "micro_story": {
        "hook_max_words": 10,
        "hook_beat_max_seconds": 3.3,
        "opening_job": "open inside one concrete scene/action; do not pre-explain the lesson",
    },
    "quote_reflection": {
        "hook_max_words": 10,
        "hook_beat_max_seconds": 3.5,
        "opening_job": "use only a short exact approved quote as the hook; if the quote is long, open with a source-grounded reflection and let the quote breathe later",
    },
}

SHORT_ON_SCREEN_MAX_WORDS = 12
SHORT_PAYOFF_MAX_WORDS = 14
SHORT_CTA_MAX_WORDS = 14
LONG_OPENING_PROMISE_TARGET_SECONDS = 7.0
LONG_DERIVATIVE_ON_SCREEN_MAX_WORDS = 10
LONG_DERIVATIVE_KEY_POINT_MAX_WORDS = 14


class ShortEditorialCraftError(RuntimeError):
    pass


def clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def word_count(value: object) -> int:
    return len(clean(value).split())


def template_spec(template: object) -> dict[str, Any]:
    key = clean(template)
    spec = _TEMPLATE_SPECS.get(key)
    if spec is None:
        raise ShortEditorialCraftError(f"unsupported_short_template:{key or 'empty'}")
    return dict(spec)


def template_hook_word_limit(template: object) -> int:
    return int(template_spec(template)["hook_max_words"])


def template_hook_beat_max_seconds(template: object) -> float:
    return float(template_spec(template)["hook_beat_max_seconds"])


def template_names() -> tuple[str, ...]:
    return tuple(_TEMPLATE_SPECS)


def craft_writing_directive() -> str:
    """Provider-visible guidance folded into the existing planning call; no new call/retry."""
    short_rules = " ".join(
        f"{name}: hook<={int(spec['hook_max_words'])} words, first visual beat<={float(spec['hook_beat_max_seconds']):.1f}s, {spec['opening_job']}."
        for name, spec in _TEMPLATE_SPECS.items()
    )
    return (
        "Editorial craft (same call, no extra generation): Moment opens immediately and keeps one calm idea per beat. "
        f"{short_rules} Keep on_screen_text <= {SHORT_ON_SCREEN_MAX_WORDS} words, payoff <= {SHORT_PAYOFF_MAX_WORDS} words, "
        f"CTA <= {SHORT_CTA_MAX_WORDS} words and one concrete action; keep the channel reflective, not hyper-cut or hype-led. "
        f"Long: expose the first tension/viewer promise in the opening sentence, roughly within {LONG_OPENING_PROMISE_TARGET_SECONDS:.0f}s, "
        f"and when feasible keep section on_screen_text <= {LONG_DERIVATIVE_ON_SCREEN_MAX_WORDS} words and key_point <= {LONG_DERIVATIVE_KEY_POINT_MAX_WORDS} words "
        "so approved sections remain clean source atoms for sibling Shorts."
    )


def merge_craft_revision_note(existing: object) -> str:
    prior = clean(existing)
    directive = craft_writing_directive()
    if directive in prior:
        return prior
    return f"{prior} {directive}" if prior else directive
