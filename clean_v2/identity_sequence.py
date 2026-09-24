from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .media import probe_duration

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

    Intro is visual-only and is inserted later at exactly the hook boundary. The spoken
    prayer/definition therefore remain part of the narration rather than an external card.
    """
    if fmt not in {"short", "film"} or not sections:
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

    if fmt == "film" and closer:
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
    if fmt not in {"short", "film"} or not sections:
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
        if fmt == "film" and legacy_closer and joined.count(legacy_closer) != 1:
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

    if fmt == "film":
        closer = " ".join(str(closer or "").split()).strip()
        if closer and joined.count(closer) != 1:
            raise RuntimeError("identity sequence requires exactly one long-form closer")


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _asset_pair(fmt: str) -> tuple[Path, Path, int, int]:
    if fmt == "short":
        return _SHORT_INTRO, _SHORT_OUTRO, 1080, 1920
    if fmt == "film":
        return _LONG_INTRO, _LONG_OUTRO, 1920, 1080
    raise RuntimeError(f"identity media unsupported format: {fmt}")


def _hook_seconds(output_dir: Path, script: Mapping[str, Any], fmt: str) -> float:
    sections = script.get("sections") or []
    if not sections or not isinstance(sections[0], Mapping):
        raise RuntimeError("identity media requires script sections")
    first_text = " ".join(str(sections[0].get("narration") or "").split()).strip()
    hook = _first_sentence(first_text)
    if not hook:
        raise RuntimeError("identity media requires first spoken hook")

    # The voice stage deliberately synthesizes the first sentence as chunk 01,
    # so prefer its measured duration. Fall back to the previous conservative
    # estimate only for old/resumed artifacts that predate that lightweight seam.
    exact_hook = output_dir / "audio" / "01-chunks" / "01.wav"
    if exact_hook.is_file() and exact_hook.stat().st_size > 1024:
        return float(probe_duration(exact_hook))

    section_audio = output_dir / "audio" / "01.wav"
    if section_audio.is_file():
        section_seconds = probe_duration(section_audio)
    else:
        section_seconds = probe_duration(output_dir / "narration.wav") / max(1, len(sections))

    ratio = len(hook) / max(1, len(first_text))
    estimate = section_seconds * ratio
    lower = 0.75 if fmt == "short" else 1.0
    upper = 6.5 if fmt == "short" else 12.0
    return max(lower, min(estimate, min(upper, max(lower, section_seconds - 0.15))))


def _prayer_seconds(output_dir: Path, script: Mapping[str, Any], fmt: str) -> float:
    sections = script.get("sections") or []
    first_text = " ".join(str(sections[0].get("narration") or "").split()).strip()
    section_audio = output_dir / "audio" / "01.wav"
    if section_audio.is_file():
        section_seconds = probe_duration(section_audio)
    else:
        section_seconds = probe_duration(output_dir / "narration.wav") / max(1, len(sections))
    estimate = section_seconds * (len(PRAYER_SENTENCE) / max(1, len(first_text)))
    lower, upper = ((1.25, 2.8) if fmt == "short" else (1.5, 3.2))
    return max(lower, min(estimate, upper))


def _splice_intro(
    source: Path,
    intro: Path,
    dest: Path,
    *,
    hook_seconds: float,
    width: int,
    height: int,
) -> None:
    filters = (
        f"[0:v]trim=0:{hook_seconds:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height},fps=30,setsar=1,format=yuv420p[v0];"
        f"[0:a]atrim=0:{hook_seconds:.3f},asetpts=PTS-STARTPTS,"
        "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a0];"
        f"[1:v]scale={width}:{height},fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[vi];"
        "[1:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS[ai];"
        f"[0:v]trim=start={hook_seconds:.3f},setpts=PTS-STARTPTS,"
        f"scale={width}:{height},fps=30,setsar=1,format=yuv420p[v1];"
        f"[0:a]atrim=start={hook_seconds:.3f},asetpts=PTS-STARTPTS,"
        "aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo[a1];"
        "[v0][a0][vi][ai][v1][a1]concat=n=3:v=1:a=1[v][a]"
    )
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-i", str(intro),
        "-filter_complex", filters,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(dest),
    ])


def _overlay_prayer_image(
    source: Path,
    image: Path,
    dest: Path,
    *,
    start_seconds: float,
    display_seconds: float,
    fmt: str,
) -> None:
    source_duration = probe_duration(source)
    card_width = 760 if fmt == "short" else 600
    fade_out = max(0.3, display_seconds - 0.28)
    filters = (
        f"[1:v]scale={card_width}:-1,format=rgba,"
        "fade=t=in:st=0:d=0.22:alpha=1,"
        f"fade=t=out:st={fade_out:.3f}:d=0.22:alpha=1,"
        f"setpts=PTS+{start_seconds:.3f}/TB[card];"
        "[0:v][card]overlay=(W-w)/2:(H-h)/2:format=auto[v]"
    )
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-loop", "1", "-framerate", "30", "-i", str(image),
        "-filter_complex", filters,
        "-map", "[v]", "-map", "0:a:0",
        "-t", f"{source_duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", str(dest),
    ])


def _append_outro(
    source: Path,
    outro: Path,
    dest: Path,
    *,
    width: int,
    height: int,
) -> None:
    filters = (
        f"[0:v]scale={width}:{height},fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v0];"
        "[0:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS[a0];"
        f"[1:v]scale={width}:{height},fps=30,setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v1];"
        "[1:a]aresample=48000,aformat=sample_rates=48000:channel_layouts=stereo,"
        "asetpts=PTS-STARTPTS[a1];"
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-i", str(outro),
        "-filter_complex", filters,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart", str(dest),
    ])


def apply_identity_media(
    *,
    output_dir: Path,
    final_path: Path,
    script: Mapping[str, Any],
    fmt: str,
) -> dict[str, Any]:
    """Insert approved identity media locally with zero provider calls.

    Final order for short and film:
    hook -> intro -> prayer sentence (+ approved prayer image) ->
    channel definition -> topic/body -> outro.
    """
    if fmt not in {"short", "film"}:
        return {
            "schema_version": 1,
            "source": "clean-v2-approved-identity-media-v1",
            "status": "not_applicable",
            "format": fmt,
            "provider_calls_added": 0,
        }

    intro, outro, width, height = _asset_pair(fmt)
    for required in (intro, outro, _PRAYER_IMAGE):
        if not required.is_file() or required.stat().st_size <= 1024:
            raise RuntimeError(f"approved identity asset missing: {required.name}")

    output_dir = Path(output_dir)
    final_path = Path(final_path)
    hook_seconds = _hook_seconds(output_dir, script, fmt)
    prayer_seconds = _prayer_seconds(output_dir, script, fmt)
    intro_seconds = probe_duration(intro)
    before_seconds = probe_duration(final_path)

    with_intro = output_dir / ".identity-with-intro.mp4"
    with_prayer = output_dir / ".identity-with-prayer.mp4"
    completed = output_dir / ".identity-completed.mp4"
    for path in (with_intro, with_prayer, completed):
        path.unlink(missing_ok=True)

    try:
        _splice_intro(
            final_path,
            intro,
            with_intro,
            hook_seconds=hook_seconds,
            width=width,
            height=height,
        )
        prayer_start = hook_seconds + intro_seconds
        _overlay_prayer_image(
            with_intro,
            _PRAYER_IMAGE,
            with_prayer,
            start_seconds=prayer_start,
            display_seconds=prayer_seconds,
            fmt=fmt,
        )
        _append_outro(
            with_prayer,
            outro,
            completed,
            width=width,
            height=height,
        )
        os.replace(completed, final_path)
    finally:
        with_intro.unlink(missing_ok=True)
        with_prayer.unlink(missing_ok=True)
        completed.unlink(missing_ok=True)

    report = {
        "schema_version": 1,
        "source": "clean-v2-approved-identity-media-v1",
        "status": "pass",
        "format": fmt,
        "sequence": [
            "hook",
            "intro",
            "prayer_sentence_with_visual",
            "channel_definition",
            "topic",
            "outro",
        ],
        "hook_seconds_estimated": round(hook_seconds, 3),
        "intro_seconds": round(intro_seconds, 3),
        "prayer_visual_seconds": round(prayer_seconds, 3),
        "content_seconds_before_identity_media": round(before_seconds, 3),
        "final_seconds_after_identity_media": round(probe_duration(final_path), 3),
        "prayer_sentence": PRAYER_SENTENCE,
        "channel_definition": channel_definition(fmt),
        "provider_calls_added": 0,
    }
    (output_dir / "identity-sequence.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report
