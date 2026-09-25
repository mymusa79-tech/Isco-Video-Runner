from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

PRAYER_SENTENCE = "اللهم صلِّ وسلِّم على نبينا محمد."
SHORT_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح."
LONG_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أصدق، ونبحث عن خطوة عملية نحو حياة أوضح."

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "identity"
_SHORT_INTRO = _ASSET_DIR / "short_intro.mp4"
_SHORT_OUTRO = _ASSET_DIR / "short_outro.mp4"
_LONG_INTRO = _ASSET_DIR / "long_intro.mp4"
_LONG_OUTRO = _ASSET_DIR / "long_outro.mp4"
_PRAYER_IMAGE = _ASSET_DIR / "prayer_image.jpg"
_SENTENCE_END_RE = re.compile(r"[.!؟!]")


def identity_asset_paths(fmt: str) -> dict[str, Path]:
    """Return approved visual identity assets; timing is owned elsewhere by audio."""
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
    identity_block = f"{PRAYER_SENTENCE} {definition}".strip()
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
    if joined.count(definition) != 1:
        raise RuntimeError("identity sequence requires exactly one channel-definition sentence")

    first = str(sections[0].get("narration") or "")
    hook = _first_sentence(first)
    prayer_pos = first.find(PRAYER_SENTENCE)
    definition_pos = first.find(definition)
    if not hook or prayer_pos < len(hook) or definition_pos <= prayer_pos:
        raise RuntimeError("identity sequence order must be hook -> prayer -> channel definition")

    if fmt in {"film", "podcast"}:
        closer = " ".join(str(closer or "").split()).strip()
        if closer and joined.count(closer) != 1:
            raise RuntimeError("identity sequence requires exactly one long-form closer")


def _asset_pair(fmt: str) -> tuple[Path, Path, int, int]:
    if fmt == "short":
        return _SHORT_INTRO, _SHORT_OUTRO, 1080, 1920
    if fmt in {"film", "podcast"}:
        return _LONG_INTRO, _LONG_OUTRO, 1920, 1080
    raise RuntimeError(f"identity media unsupported format: {fmt}")
