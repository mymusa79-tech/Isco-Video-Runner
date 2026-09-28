from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

PRAYER_SENTENCE = "اللهم صلِّ وسلِّم على نبينا محمد."
SHORT_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح."
LONG_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أصدق، ونبحث عن خطوة عملية نحو حياة أوضح."
PODCAST_CHANNEL_DEFINITION = "بودكاست من نداء اليقظة"

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "identity"
_SHORT_INTRO = _ASSET_DIR / "short_intro.mp4"
_SHORT_OUTRO = _ASSET_DIR / "short_outro.mp4"
_LONG_INTRO = _ASSET_DIR / "long_intro.mp4"
_LONG_OUTRO = _ASSET_DIR / "long_outro.mp4"
_PODCAST_INTRO = _ASSET_DIR / "podcast_intro.mp4"
_PODCAST_OUTRO = _ASSET_DIR / "podcast_outro.mp4"
_PRAYER_IMAGE = _ASSET_DIR / "prayer_image.jpg"
_SENTENCE_END_RE = re.compile(r"[.!؟!]")
_SUPPORTED_IDENTITY_FORMATS = frozenset({"short", "film", "podcast"})
_TIMING_PROFILES = {
    # Structural breathing is deliberate and provider-independent. It is inserted
    # only at major opening boundaries, never inside ordinary Gemini speech.
    # Long-form gets slightly more room than Shorts without becoming sluggish.
    "short": {
        "post_hook_silence_seconds": 1.15,
        "intro_silence_seconds": 1.15,
        "post_prayer_silence_seconds": 0.35,
        "pre_topic_silence_seconds": 0.65,
        "final_silence_seconds": 2.20,
    },
    "film": {
        "post_hook_silence_seconds": 1.15,
        "intro_silence_seconds": 2.20,
        "post_prayer_silence_seconds": 0.45,
        "pre_topic_silence_seconds": 0.85,
        "final_silence_seconds": 3.50,
    },
    "podcast": {
        # Outside the Text has its own deliberate podcast rhythm:
        # listener A asks -> breath -> 6s branded intro -> prayer -> breath ->
        # Charon answers directly in the topic. Body music starts with that answer.
        "post_hook_silence_seconds": 0.75,
        "intro_silence_seconds": 6.00,
        "post_prayer_silence_seconds": 0.65,
        # Kept in the shared profile schema but intentionally unused for podcast:
        # there is no extra spoken channel-definition beat after the prayer.
        "pre_topic_silence_seconds": 0.00,
        "final_silence_seconds": 6.50,
    },
}


def identity_timing_profile(fmt: str) -> dict[str, float]:
    if fmt not in _SUPPORTED_IDENTITY_FORMATS:
        raise RuntimeError(f"identity timing unsupported format: {fmt}")
    return dict(_TIMING_PROFILES[fmt])


def identity_asset_paths(
    fmt: str,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Path]:
    """Return fixed local identity assets; no runtime card generation or provider call."""
    del runtime_dir
    intro, outro, _width, _height = _asset_pair(fmt)
    return {"intro": intro, "prayer": _PRAYER_IMAGE, "outro": outro}


def _first_sentence(text: str) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    match = _SENTENCE_END_RE.search(compact)
    return compact[: match.end()].strip() if match else compact


def channel_definition(fmt: str, opener: str = "") -> str:
    if fmt == "short":
        return SHORT_CHANNEL_DEFINITION
    if fmt == "podcast":
        # Visual V8 intro owns the series/channel branding. This value remains
        # available as metadata but is intentionally not inserted into speech.
        return PODCAST_CHANNEL_DEFINITION
    candidate = " ".join(str(opener or "").split()).strip()
    return candidate or LONG_CHANNEL_DEFINITION


def inject_spoken_identity(
    sections: list[dict[str, Any]],
    *,
    fmt: str,
    opener: str = "",
    closer: str = "",
) -> None:
    """Keep the approved spoken order: hook -> prayer -> channel definition -> topic.

    Visual identity is timed later from measured voice-unit boundaries inside Timeline First;
    nothing is appended after the final render or allowed to extend narration duration.
    """
    if fmt not in {"short", "film", "podcast"} or not sections:
        return

    definition = channel_definition(fmt, opener)
    identity_block = (
        PRAYER_SENTENCE
        if fmt == "podcast"
        else f"{PRAYER_SENTENCE} {definition}".strip()
    )
    closer = " ".join(str(closer or "").split()).strip()

    for section in sections:
        narration = " ".join(str(section.get("narration") or "").split()).strip()
        for phrase in (PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, LONG_CHANNEL_DEFINITION, definition, closer):
            if phrase:
                narration = " ".join(narration.replace(phrase, " ").split()).strip()
        section["narration"] = narration

    first = str(sections[0].get("narration") or "").strip()
    hook = _first_sentence(first)
    if not hook:
        raise RuntimeError("identity sequence requires a non-empty first-sentence hook")
    remainder = first[len(hook):].lstrip()
    # Keep a hard sentence boundary before the host-owned prayer. Providers
    # sometimes return a valid hook without terminal punctuation; without this
    # boundary the prayer's final period becomes the first sentence terminator,
    # so the downstream invariant incorrectly treats hook+prayer as one sentence.
    if not _SENTENCE_END_RE.search(hook[-1:]):
        hook = f"{hook}."
    sections[0]["narration"] = f"{hook} {identity_block} {remainder}".strip()

    if fmt in {"film", "podcast"} and closer:
        sections[-1]["narration"] = (
            f"{str(sections[-1].get('narration') or '').rstrip()} {closer}"
        ).strip()


def assert_spoken_identity(
    sections: list[dict[str, Any]],
    *,
    fmt: str,
    opener: str = "",
    closer: str = "",
) -> None:
    if fmt not in {"short", "film", "podcast"} or not sections:
        return

    definition = channel_definition(fmt, opener)
    joined = "\n".join(str(item.get("narration") or "") for item in sections)

    # Backward-compatible seam for direct repair/unit fixtures that exercise the
    # older opener/closer contract without running the production identity injector.
    # Real pipeline scripts always contain PRAYER_SENTENCE before this invariant.
    if PRAYER_SENTENCE not in joined:
        legacy_opener = " ".join(str(opener or "").split()).strip()
        legacy_closer = " ".join(str(closer or "").split()).strip()
        if legacy_opener and joined.count(legacy_opener) != 1:
            raise RuntimeError("legacy identity requires exactly one opener")
        if fmt in {"film", "podcast"} and legacy_closer and joined.count(legacy_closer) != 1:
            raise RuntimeError("legacy identity requires exactly one closer")
        return

    if joined.count(PRAYER_SENTENCE) != 1:
        raise RuntimeError("identity sequence requires exactly one approved prayer sentence")
    if fmt != "podcast" and joined.count(definition) != 1:
        raise RuntimeError("identity sequence requires exactly one channel-definition sentence")

    first = str(sections[0].get("narration") or "")
    hook = _first_sentence(first)
    prayer_pos = first.find(PRAYER_SENTENCE)
    if not hook or prayer_pos < len(hook):
        raise RuntimeError("identity sequence order must begin hook -> prayer")
    if fmt != "podcast":
        definition_pos = first.find(definition)
        if definition_pos <= prayer_pos:
            raise RuntimeError("identity sequence order must be hook -> prayer -> channel definition")

    if fmt in {"film", "podcast"}:
        closer = " ".join(str(closer or "").split()).strip()
        if closer and joined.count(closer) != 1:
            raise RuntimeError("identity sequence requires exactly one long-form closer")


def _asset_pair(fmt: str) -> tuple[Path, Path, int, int]:
    if fmt == "short":
        return _SHORT_INTRO, _SHORT_OUTRO, 1080, 1920
    if fmt == "film":
        return _LONG_INTRO, _LONG_OUTRO, 1920, 1080
    if fmt == "podcast":
        return _PODCAST_INTRO, _PODCAST_OUTRO, 1920, 1080
    raise RuntimeError(f"identity media unsupported format: {fmt}")
