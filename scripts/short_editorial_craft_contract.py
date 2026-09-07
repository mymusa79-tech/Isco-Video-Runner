from __future__ import annotations

from typing import Any


# Run219 editorial closure. These are zero-provider-call craft targets: they shape the
# existing Producer request and the local post-voice visual timeline only.
# voice_mode mirrors the spoken-beat selection short_voice_v2.decide_voice_mode() and
# short_voice_owned_timeline._performance_script() both use: "hybrid" speaks only the
# hook+payoff beats (the middle beats stay visual/on-screen-only), "voice_led" speaks
# every approved beat. This is the single source of truth both modules delegate to -
# duplicating this mapping caused exactly the kind of drift risk this contract exists
# to prevent.
_TEMPLATE_SPECS: dict[str, dict[str, Any]] = {
    "why_reframe": {
        "hook_max_words": 8,
        "hook_beat_max_seconds": 3.0,
        "opening_job": "contrast/reframe",
        "voice_mode": "hybrid",
    },
    "inner_dialogue": {
        "hook_max_words": 9,
        "hook_beat_max_seconds": 3.2,
        "opening_job": "inner thought then turn",
        "voice_mode": "voice_led",
    },
    "micro_story": {
        "hook_max_words": 10,
        "hook_beat_max_seconds": 3.3,
        "opening_job": "concrete scene then turn",
        "voice_mode": "voice_led",
    },
    "quote_reflection": {
        "hook_max_words": 10,
        "hook_beat_max_seconds": 3.5,
        "opening_job": "approved quote/reflection only",
        "voice_mode": "hybrid",
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


def template_voice_mode(template: object) -> str:
    return str(template_spec(template)["voice_mode"])


def template_names() -> tuple[str, ...]:
    return tuple(_TEMPLATE_SPECS)


def short_writing_directive() -> str:
    """Compact Moment-only guidance; timeline seconds are enforced locally later."""
    return (
        "Craft (same call; no extra generation): Moment one calm idea/beat; "
        "why_reframe hook<=8w contrast/reframe; inner_dialogue<=9w thought->turn; "
        "micro_story<=10w scene->turn; quote_reflection<=10w approved quote only; "
        f"on_screen<={SHORT_ON_SCREEN_MAX_WORDS}w; payoff<={SHORT_PAYOFF_MAX_WORDS}w; "
        f"CTA<={SHORT_CTA_MAX_WORDS}w one action; reflective, no hype."
    )


def long_writing_directive() -> str:
    """Long-only guidance kept intentionally tiny to preserve split-outline redundancy."""
    return (
        "Craft (same call; no extra generation): Long opening tension/promise ~"
        f"{LONG_OPENING_PROMISE_TARGET_SECONDS:.0f}s; when feasible on_screen<={LONG_DERIVATIVE_ON_SCREEN_MAX_WORDS}w; "
        f"key_point<={LONG_DERIVATIVE_KEY_POINT_MAX_WORDS}w for source-derived Shorts."
    )


def craft_writing_directive(requested_format: object = "") -> str:
    """Route only format-relevant craft into the existing provider call."""
    fmt = clean(requested_format).lower()
    if fmt == "moment":
        return short_writing_directive()
    if fmt in {"film", "story"}:
        return long_writing_directive()
    # Unknown/legacy callers must not receive mutually unrelated Short+Long craft.
    # The Producer's common deterministic guidance remains available, while live
    # production always supplies the requested format before provider transport.
    return ""


def merge_craft_revision_note(existing: object, requested_format: object = "") -> str:
    prior = clean(existing)
    directive = craft_writing_directive(requested_format)
    if not directive or directive in prior:
        return prior
    return f"{prior} {directive}" if prior else directive
