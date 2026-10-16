"""Full burned-in Podcast captions, coloured per speaker.

The Podcast is a two-voice conversation, so a viewer watching without sound must see
who is talking: A (the listener's question) in gold, B (the answer) in warm white.

Timing is owned by the measured audio, never guessed from the section:
  * every spoken chunk has a measured start/end in ``timeline-first.json``;
  * the chunk text is saved next to its audio in ``voice-sections.json``;
  * inside one chunk the phrase boundaries are snapped to the real pauses found in that
    chunk's own audio file (ffmpeg silencedetect), falling back to character weights.
This is not word-level speech alignment and the report says so.

Captions are shown only over the hook question and the topic. The fixed intro, prayer,
channel definition, CTA and outro stay clean, and the sparse key text is not drawn on
top of them (the pipeline skips it when these captions are burned).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from .short_timed_text import (
    BODY_FONT,
    OUTLINE_ASS,
    PRIMARY_ASS,
    _ass_time,
    _caption_rows_and_size,
    _filter_escape_path,
    _rtl_row,
    is_verbatim_split,
    resolved_font,
    _secret_free_subprocess_env,
    _speaker_chunks,
    _srt_time,
)

CLEAN_MASTER_NAME = "final-no-captions.mp4"
SCHEMA_VERSION = 1
CAPTION_ROLES = frozenset({"hook", "topic"})  # cta_topic keeps its own on-screen CTA
SPEAKER_HEX = {"A": "#C9A24B", "B": "#F5F3EF"}
SPEAKER_ASS = {"A": r"\c&H4BA2C9&", "B": r"\c&HEFF3F5&"}
FONT_SIZE = 64  # 5.9% of the 1080 px frame; two rows still fit the safe width
MARGIN_V = 150
MIN_EVENT_SECONDS = 0.5
SILENCE_NOISE_DB = -32
SILENCE_MIN_SECONDS = 0.18


class PodcastCaptionError(RuntimeError):
    pass


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _silence_midpoints(audio: Path) -> list[float]:
    """Pause centres (seconds, relative to the file) found in one chunk's audio."""
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
                "-af", f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_SECONDS}",
                "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=300, check=False,
            env=_secret_free_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    starts = [float(v) for v in re.findall(r"silence_start:\s*(-?[0-9.]+)", completed.stderr)]
    ends = [float(v) for v in re.findall(r"silence_end:\s*(-?[0-9.]+)", completed.stderr)]
    return [(max(s, 0.0) + e) / 2 for s, e in zip(starts, ends) if e > s]


def align_boundaries(weights: Sequence[int], duration: float, pauses: Sequence[float]) -> list[float]:
    """Cut points (len(weights)+1, from 0 to duration), snapped to real pauses when close.

    Each cut starts from the character-weighted estimate over the time that is still left
    (so a snap re-anchors the rest instead of letting error pile up) and moves to a detected
    pause only if one lies within about half a phrase of the estimate. A far-away pause is
    more likely another sentence gap than this boundary, so it is ignored.
    """
    cuts = [0.0]
    count = len(weights)
    for index in range(count - 1):
        remaining_weight = float(sum(weights[index:])) or 1.0
        remaining_time = duration - cuts[-1]
        phrase = remaining_time * weights[index] / remaining_weight
        target = cuts[-1] + phrase
        tolerance = min(2.5, max(0.6, 0.45 * phrase))
        floor = cuts[-1] + MIN_EVENT_SECONDS
        ceiling = duration - MIN_EVENT_SECONDS * (count - index - 1)
        near = [p for p in pauses if floor <= p <= ceiling and abs(p - target) <= tolerance]
        cut = min(near, key=lambda p: abs(p - target)) if near else target
        cuts.append(min(max(cut, floor), max(floor, ceiling)))
    cuts.append(duration)
    return cuts


def align_phrases(
    tagged: Sequence[tuple[str, str]], duration: float, pauses: Sequence[float]
) -> list[tuple[str, str, float, float]]:
    """(speaker, phrase, start, end) inside one chunk.

    Turn changes are aligned first, against all the pauses: that is where the audio has a
    reliable gap. Phrase cuts inside a turn then use only the pauses that fall inside that turn,
    so a turn-change pause can never be mistaken for a mid-sentence boundary.
    """
    turns: list[list[int]] = []
    for index, (speaker, _chunk) in enumerate(tagged):
        if turns and tagged[turns[-1][-1]][0] == speaker:
            turns[-1].append(index)
        else:
            turns.append([index])
    turn_weights = [sum(max(1, len(tagged[i][1])) for i in group) for group in turns]
    turn_cuts = align_boundaries(turn_weights, duration, pauses)
    placed: list[tuple[str, str, float, float]] = []
    for group, left, right in zip(turns, turn_cuts, turn_cuts[1:]):
        inner = [p - left for p in pauses if left + 0.3 < p < right - 0.3]
        cuts = align_boundaries([max(1, len(tagged[i][1])) for i in group], right - left, inner)
        for i, a, b in zip(group, cuts, cuts[1:]):
            placed.append((tagged[i][0], tagged[i][1], left + a, left + b))
    return placed


def build_events(output_dir: Path, *, fmt: str = "podcast", caption_roles=CAPTION_ROLES) -> tuple[list[dict[str, Any]], str]:
    root = Path(output_dir)
    timeline = _read_json(root / "timeline-first.json")
    voice = _read_json(root / "voice-sections.json")
    if not isinstance(timeline, Mapping) or timeline.get("status") != "pass":
        raise PodcastCaptionError("podcast_captions_timeline_missing")
    if not isinstance(voice, Mapping):
        raise PodcastCaptionError("podcast_captions_voice_report_missing")
    texts: dict[tuple[str, int], str] = {}
    for section in voice.get("sections") or []:
        if not isinstance(section, Mapping):
            continue
        for chunk in section.get("chunks") or []:
            if isinstance(chunk, Mapping) and chunk.get("text"):
                texts[(str(section.get("id")), int(chunk.get("chunk") or 0))] = str(chunk["text"])

    events: list[dict[str, Any]] = []
    snapped = 0
    for unit in timeline.get("audio_units") or []:
        if not isinstance(unit, Mapping) or str(unit.get("role")) not in caption_roles:
            continue
        text = texts.get((str(unit.get("section_id")), int(unit.get("chunk") or 0)))
        if not text:
            raise PodcastCaptionError("podcast_captions_chunk_text_missing")
        start, end = float(unit["start"]), float(unit["end"])
        if end - start < MIN_EVENT_SECONDS:
            continue
        tagged = [(speaker, chunk) for speaker, chunk in _speaker_chunks(text, fmt) if chunk.strip()]
        if not tagged:
            continue
        if not is_verbatim_split([chunk for _speaker, chunk in tagged], text):
            # The approved text is the only truth: a caption set that added, lost or changed
            # a word is never drawn.
            raise PodcastCaptionError("podcast_captions_text_not_verbatim")
        audio = root / str(unit.get("file") or "")
        pauses = _silence_midpoints(audio) if audio.is_file() else []
        scale = (end - start) / max(float(unit.get("duration_seconds") or (end - start)), 1e-6)
        pauses = [p * scale for p in pauses]
        placed = align_phrases(tagged, end - start, pauses)
        if pauses and any(abs(a - p) < 0.05 for _s, _c, a, _b in placed[1:] for p in pauses):
            snapped += 1
        for speaker, chunk, left, right in placed:
            if right - left > 0.01:
                events.append({
                    "start": round(start + left, 3), "end": round(start + right, 3),
                    "text": chunk, "speaker": speaker,
                })
    mode = "measured_audio_units_snapped_to_pauses" if snapped else "measured_audio_units_char_weighted"
    return events, mode


def build_ass(events: Sequence[Mapping[str, Any]], *, fmt: str = "podcast") -> str:
    width, height, font_size, margin = (1080, 1920, 92, 430) if fmt == "short" else (1920, 1080, FONT_SIZE, MARGIN_V)
    side_margin = 90 if fmt == "short" else 140
    lines = [
        "[Script Info]", "ScriptType: v4.00+", "WrapStyle: 2", "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}", f"PlayResY: {height}", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Caption,{BODY_FONT},{font_size},{PRIMARY_ASS},{PRIMARY_ASS},{OUTLINE_ASS},&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,{side_margin},{side_margin},{margin},-1",
        "", "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for item in events:
        rows, size = _caption_rows_and_size(str(item["text"]), fmt=fmt, font_size=font_size)
        if not rows:
            continue
        rendered = r"\N".join(_rtl_row(row.split()) for row in rows)
        colour = SPEAKER_ASS.get(str(item.get("speaker") or ""), "")
        lines.append(
            f'Dialogue: 0,{_ass_time(float(item["start"]))},{_ass_time(float(item["end"]))},Caption,,0,0,0,,'
            + r"{\fs" + str(size) + r"\fad(100,120)" + colour + "}" + rendered
        )
    lines.append("")
    return "\n".join(lines)


def build_srt(events: Sequence[Mapping[str, Any]]) -> str:
    blocks = []
    for index, item in enumerate(events, 1):
        rows, _size = _caption_rows_and_size(str(item["text"]), fmt="podcast", font_size=FONT_SIZE)
        if rows:
            blocks.append(
                f'{index}\n{_srt_time(float(item["start"]))} --> {_srt_time(float(item["end"]))}\n'
                + "\n".join(rows) + "\n"
            )
    return "\n".join(blocks) + "\n"


def apply_podcast_captions(*, output_dir: Path, final_path: Path) -> dict[str, Any]:
    root = Path(output_dir)
    events, timing = build_events(root)
    if not events:
        return {"status": "skipped_no_caption_windows", "format": "podcast", "provider_calls_added": 0}
    ass_path = root / "podcast-captions.ass"
    srt_path = root / "podcast-captions.srt"
    ass_path.write_text(build_ass(events), encoding="utf-8")
    srt_path.write_text(build_srt(events), encoding="utf-8")
    rendered = root / ".final-podcast-captions.mp4"
    try:
        completed = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "verbose",
                "-i", str(Path(final_path).resolve()),
                "-vf", f"ass='{_filter_escape_path(ass_path)}':shaping=complex",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "copy", str(rendered.resolve()),
            ],
            check=True, timeout=1800, env=_secret_free_subprocess_env(),
            capture_output=True, text=True,
        )
        font = resolved_font(completed.stderr, BODY_FONT)
        if not font["is_requested_font"]:
            print(f"::warning title=podcast-captions-font::libass drew with {font['resolved_name']}, not {BODY_FONT}")
        if not rendered.is_file() or rendered.stat().st_size <= 0:
            raise PodcastCaptionError("podcast_captions_render_missing_or_empty")
        # Keep the caption-free master: the derived Short is cut from it so the long
        # captions are never burned a second time inside the 9:16 clip (and its blurred
        # background). Not a delivered file and not part of the primary hash set.
        shutil.copy2(Path(final_path), root / CLEAN_MASTER_NAME)
        os.replace(rendered, Path(final_path))
    except PodcastCaptionError:
        rendered.unlink(missing_ok=True)
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        rendered.unlink(missing_ok=True)
        raise PodcastCaptionError(f"podcast_captions_render_failed:{type(exc).__name__}") from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "pass",
        "format": "podcast",
        "burned_into_final_video": True,
        "event_count": len(events),
        "speaker_colors": SPEAKER_HEX,
        "font": BODY_FONT,
        "font_actually_used": font,
        "text_policy": "approved script words only; split into phrases, verified word-for-word before render",
        "font_weight": "bold",
        "max_rows": 2,
        "position": "bottom_safe_area",
        "timing": timing,
        "timing_note": "chunk windows are measured audio; phrase cuts inside a chunk are snapped to detected pauses, not word-level speech alignment",
        "clean_windows": "intro, prayer, channel definition, CTA and outro have no dialogue captions",
        "sidecars": [srt_path.name, ass_path.name],
        "provider_calls_added": 0,
        "new_quality_gate": False,
    }
