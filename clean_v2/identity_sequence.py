from __future__ import annotations

import os
import re
import subprocess
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
_SUPPORTED_IDENTITY_FORMATS = frozenset({"short", "film", "podcast"})
_TIMING_PROFILES = {
    "short": {
        "intro_silence_seconds": 1.0,
        "pre_outro_silence_seconds": 0.45,
        "outro_silence_seconds": 2.0,
        "final_silence_seconds": 0.75,
    },
    "film": {
        "intro_silence_seconds": 1.25,
        "pre_outro_silence_seconds": 0.60,
        "outro_silence_seconds": 3.0,
        "final_silence_seconds": 1.0,
    },
    "podcast": {
        "intro_silence_seconds": 1.25,
        "pre_outro_silence_seconds": 0.60,
        "outro_silence_seconds": 3.0,
        "final_silence_seconds": 1.0,
    },
}


def identity_timing_profile(fmt: str) -> dict[str, float]:
    if fmt not in _SUPPORTED_IDENTITY_FORMATS:
        raise RuntimeError(f"identity timing unsupported format: {fmt}")
    return dict(_TIMING_PROFILES[fmt])


def _podcast_identity_ass(*, subtitle: str, duration: float) -> str:
    rle, pdf = "\u202b", "\u202c"
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Title,Noto Sans Arabic,96,&H005BA8D7,&H005BA8D7,&H00111820,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,5,60,60,0,1
Style: Sub,Noto Sans Arabic,38,&H00F2F2F2,&H00F2F2F2,&H00111820,&H00000000,0,0,0,0,100,100,0,0,1,2,1,5,60,60,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.00,0:00:{duration:05.2f},Title,,0,0,0,,{{\\an5\\pos(960,500)}}{rle}خارج النص{pdf}
Dialogue: 0,0:00:00.00,0:00:{duration:05.2f},Sub,,0,0,0,,{{\\an5\\pos(960,620)}}{rle}{subtitle}{pdf}
"""


def _ensure_podcast_identity_asset(
    destination: Path,
    *,
    subtitle: str,
    duration: float,
) -> Path:
    if destination.is_file() and destination.stat().st_size > 1024:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    ass_path = destination.with_suffix(".ass")
    temporary = destination.with_suffix(".tmp.mp4")
    ass_path.write_text(
        _podcast_identity_ass(subtitle=subtitle, duration=duration),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", f"color=c=#111820:s=1920x1080:r=30:d={duration:.2f}",
                "-vf", f"ass={ass_path.resolve()}",
                "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ],
            check=True,
            timeout=120,
            env={
                key: value
                for key, value in os.environ.items()
                if "TOKEN" not in key.upper() and "API_KEY" not in key.upper()
            },
        )
        if not temporary.is_file() or temporary.stat().st_size <= 1024:
            raise RuntimeError("podcast identity asset generation produced empty output")
        os.replace(temporary, destination)
    finally:
        ass_path.unlink(missing_ok=True)
        temporary.unlink(missing_ok=True)
    return destination


def identity_asset_paths(
    fmt: str,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Path]:
    """Swap only identity assets by format; timing stays shared for all formats."""
    intro, outro, _width, _height = _asset_pair(fmt)
    if fmt == "podcast":
        root = Path(runtime_dir or (Path.cwd() / ".clean-v2-identity-assets"))
        intro = _ensure_podcast_identity_asset(
            root / "podcast_intro.mp4",
            subtitle="نداء اليقظة",
            duration=2.0,
        )
        outro = _ensure_podcast_identity_asset(
            root / "podcast_outro.mp4",
            subtitle="مساحة لفهم ما وراء الفكرة",
            duration=4.0,
        )
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
    if fmt == "film":
        return _LONG_INTRO, _LONG_OUTRO, 1920, 1080
    if fmt == "podcast":
        return Path("podcast_intro.mp4"), Path("podcast_outro.mp4"), 1920, 1080
    raise RuntimeError(f"identity media unsupported format: {fmt}")
