from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .contracts import atomic_write_json


SCHEMA_VERSION = 1
RENDERER_VERSION = "clean-v2-cover-lite-v1"
SUPPORTED_FORMATS = frozenset({"short", "film", "podcast"})
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})

GOLD_ASS = "&H005BA8D7"
WHITE_ASS = "&H00FFFFFF"
EDGE_ASS = "&H00120F0B"
EXTRUSION_ASS = "&H00231A12"
SHADOW_ASS = "&HA8000000"

_ARABIC_STOPWORDS = frozenset(
    {
        "في",
        "من",
        "على",
        "إلى",
        "عن",
        "مع",
        "أن",
        "إن",
        "ثم",
        "أو",
        "بل",
        "لكن",
        "هذا",
        "هذه",
        "ذلك",
        "التي",
        "الذي",
        "ما",
        "لا",
        "لم",
        "لن",
        "كل",
        "فقط",
    }
)


class CoverLiteError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _compact_fallback(value: object, *, maximum_words: int = 5) -> str:
    text = _clean(value)
    words = text.split()
    if not words:
        return ""
    if len(words) <= maximum_words:
        return text
    return " ".join(words[:maximum_words]).rstrip("،؛:.-!?؟")


def resolve_cover_text(
    plan: Mapping[str, Any],
    *,
    section_id: str | None = None,
) -> str:
    sections = plan.get("sections") if isinstance(plan, Mapping) else None
    rows = [item for item in (sections or []) if isinstance(item, Mapping)]

    if section_id:
        for item in rows:
            if str(item.get("id") or "") != str(section_id):
                continue
            authored = _clean(item.get("cover_text"))
            if authored:
                return authored
            heading = _compact_fallback(item.get("heading"))
            if heading:
                return heading

    authored = _clean(plan.get("cover_text"))
    if authored:
        return authored

    if rows:
        first_authored = _clean(rows[0].get("cover_text"))
        if first_authored:
            return first_authored

    return _compact_fallback(plan.get("title")) or _compact_fallback(plan.get("promise"))


def _read_rights(output_dir: Path) -> list[Mapping[str, Any]]:
    path = Path(output_dir) / "rights-manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    assets = payload.get("assets") if isinstance(payload, Mapping) else None
    return [item for item in (assets or []) if isinstance(item, Mapping)]


def _asset_path(output_dir: Path, item: Mapping[str, Any]) -> Path | None:
    local_file = str(item.get("local_file") or "").strip()
    if not local_file:
        return None
    candidate = Path(output_dir) / "visuals" / Path(local_file).name
    try:
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    except OSError:
        return None
    return None


def select_cover_source(
    output_dir: Path,
    *,
    section_id: str | None = None,
    final_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    scored: list[tuple[int, Path, Mapping[str, Any]]] = []
    for index, item in enumerate(_read_rights(output_dir)):
        path = _asset_path(output_dir, item)
        if path is None:
            continue
        score = max(0, 20 - index)
        if section_id and str(item.get("section_id") or "") == str(section_id):
            score += 100
        if str(item.get("role") or "") == "hook":
            score += 50
        if str(item.get("source_actual") or "") == "ai_still":
            score += 20
        if not item.get("pacing_auxiliary"):
            score += 10
        scored.append((score, path, item))

    if scored:
        score, path, item = max(scored, key=lambda row: row[0])
        return path, {
            "selection": "existing_visual_asset",
            "score": score,
            "section_id": str(item.get("section_id") or ""),
            "beat_id": str(item.get("beat_id") or ""),
            "role": str(item.get("role") or ""),
            "source_actual": str(item.get("source_actual") or ""),
        }

    fallback = Path(final_path) if final_path is not None else Path(output_dir) / "final.mp4"
    try:
        if fallback.is_file() and fallback.stat().st_size > 0:
            return fallback, {"selection": "final_video_frame"}
    except OSError:
        pass
    raise CoverLiteError("cover_source_missing")


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise CoverLiteError(f"required_executable_missing:{command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CoverLiteError(f"cover_render_timeout:{command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = " ".join((exc.stderr or "").split())[-600:]
        raise CoverLiteError(f"cover_render_failed:{detail}") from None


def _probe_dimensions(path: Path) -> tuple[int, int]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(path),
        ],
        timeout=60,
    )
    match = re.fullmatch(r"(\d+)x(\d+)", result.stdout.strip())
    if not match:
        raise CoverLiteError("cover_source_dimensions_invalid")
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise CoverLiteError("cover_source_dimensions_invalid")
    return width, height


def _ass_escape(text: str) -> str:
    return _clean(text).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _focus_index(text: str) -> int:
    words = _clean(text).split()
    if not words:
        return 0
    candidates: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        bare = re.sub(r"[^\w\u0600-\u06FF]+", "", word, flags=re.UNICODE)
        if bare and bare not in _ARABIC_STOPWORDS:
            candidates.append((len(bare), index))
    return max(candidates)[1] if candidates else len(words) - 1


def _balanced_rows(text: str, *, maximum_rows: int = 2) -> list[list[str]]:
    words = _clean(text).split()
    if not words:
        return []
    if maximum_rows <= 1 or len(words) <= 3:
        return [words]
    split = (len(words) + 1) // 2
    return [words[:split], words[split:]]


def _face_text(text: str) -> str:
    words = _clean(text).split()
    if not words:
        return ""
    focus = _focus_index(text)
    rendered: list[str] = []
    for index, word in enumerate(words):
        escaped = _ass_escape(word)
        if index == focus:
            rendered.append("{\\c" + GOLD_ASS + "}" + escaped + "{\\c" + WHITE_ASS + "}")
        else:
            rendered.append(escaped)

    rows = _balanced_rows(" ".join(rendered))
    row_text = []
    cursor = 0
    for row in rows:
        count = len(row)
        row_text.append(r"\h\h".join(rendered[cursor : cursor + count]))
        cursor += count
    return "\u202B" + r"\N".join(row_text) + "\u202C"


def _plain_text(text: str) -> str:
    rows = _balanced_rows(text)
    return "\u202B" + r"\N".join(
        r"\h\h".join(_ass_escape(word) for word in row)
        for row in rows
    ) + "\u202C"


def _profile(fmt: str) -> dict[str, int | str]:
    if fmt == "short":
        return {
            "width": 1080,
            "height": 1920,
            "font_size": 132,
            "x": 540,
            "y": 420,
            "margin_v": 0,
            "profile": "vertical",
        }
    if fmt == "podcast":
        return {
            "width": 1280,
            "height": 720,
            "font_size": 74,
            "x": 640,
            "y": 205,
            "margin_v": 0,
            "profile": "podcast_landscape",
        }
    if fmt == "film":
        return {
            "width": 1280,
            "height": 720,
            "font_size": 78,
            "x": 640,
            "y": 205,
            "margin_v": 0,
            "profile": "landscape",
        }
    raise CoverLiteError(f"cover_format_unsupported:{fmt}")


def _write_ass(text: str, destination: Path, *, fmt: str) -> dict[str, Any]:
    profile = _profile(fmt)
    width = int(profile["width"])
    height = int(profile["height"])
    font_size = int(profile["font_size"])
    x = int(profile["x"])
    y = int(profile["y"])

    face = _face_text(text)
    plain = _plain_text(text)
    if not face or not plain:
        raise CoverLiteError("cover_text_missing")

    ass = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Shadow,Noto Sans Arabic,{font_size},{WHITE_ASS},{WHITE_ASS},{SHADOW_ASS},{SHADOW_ASS},-1,0,0,0,100,100,0,0,1,0,0,5,40,40,0,1
Style: Extrusion,Noto Sans Arabic,{font_size},{EXTRUSION_ASS},{EXTRUSION_ASS},{EXTRUSION_ASS},{EXTRUSION_ASS},-1,0,0,0,100,100,0,0,1,2,0,5,40,40,0,1
Style: Face,Noto Sans Arabic,{font_size},{WHITE_ASS},{WHITE_ASS},{EDGE_ASS},{EDGE_ASS},-1,0,0,0,100,100,0,0,1,3,0,5,40,40,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.00,0:00:30.00,Shadow,,0,0,0,,{{\\pos({x+7},{y+8})}}{plain}
Dialogue: 1,0:00:00.00,0:00:30.00,Extrusion,,0,0,0,,{{\\pos({x+3},{y+3})}}{plain}
Dialogue: 2,0:00:00.00,0:00:30.00,Face,,0,0,0,,{{\\pos({x},{y})}}{face}
"""
    destination.write_text(ass, encoding="utf-8")
    return dict(profile)


def _filter_escape_path(path: Path) -> str:
    return (
        str(path.resolve())
        .replace("\\", "/")
        .replace(":", r"\:")
        .replace("'", r"\'")
    )


def render_cover(
    source: Path,
    destination: Path,
    *,
    text: str,
    fmt: str,
) -> dict[str, Any]:
    source = Path(source)
    destination = Path(destination)
    if fmt not in SUPPORTED_FORMATS:
        raise CoverLiteError(f"cover_format_unsupported:{fmt}")
    if not source.is_file() or source.stat().st_size <= 0:
        raise CoverLiteError("cover_source_missing")

    destination.parent.mkdir(parents=True, exist_ok=True)
    ass_path = destination.with_name(f".{destination.stem}.cover-lite.ass")
    profile = _write_ass(text, ass_path, fmt=fmt)
    width = int(profile["width"])
    height = int(profile["height"])
    source_width, source_height = _probe_dimensions(source)
    subtitles = f"subtitles='{_filter_escape_path(ass_path)}'"

    if fmt == "short" and source_width >= source_height:
        filter_graph = (
            "[0:v]split=2[bg0][fg0];"
            f"[bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[bg];"
            f"[fg0]scale={width - 40}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,"
            "eq=contrast=1.05:saturation=0.94,"
            f"{subtitles}[v]"
        )
    else:
        filter_graph = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            "eq=contrast=1.05:saturation=0.94,"
            f"{subtitles}[v]"
        )

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if source.suffix.lower() not in IMAGE_SUFFIXES:
        command.extend(["-ss", "0.500"])
    command.extend(
        [
            "-i",
            str(source),
            "-filter_complex",
            filter_graph,
            "-map",
            "[v]",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(destination),
        ]
    )
    try:
        _run(command)
    finally:
        ass_path.unlink(missing_ok=True)

    if not destination.is_file() or destination.stat().st_size <= 0:
        raise CoverLiteError("cover_output_missing")
    return {
        "width": width,
        "height": height,
        "profile": str(profile["profile"]),
        "source_width": source_width,
        "source_height": source_height,
    }


def build_cover_lite(
    *,
    output_dir: Path,
    plan: Mapping[str, Any],
    fmt: str,
    final_path: Path,
    output_name: str = "cover.jpg",
    section_id: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    fmt = str(fmt or "").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise CoverLiteError(f"cover_format_unsupported:{fmt}")

    effective_section = section_id
    if fmt == "short" and not effective_section:
        sections = plan.get("sections") if isinstance(plan, Mapping) else None
        if isinstance(sections, list) and sections and isinstance(sections[0], Mapping):
            effective_section = str(sections[0].get("id") or "") or None

    text = resolve_cover_text(plan, section_id=effective_section)
    if not text:
        raise CoverLiteError("cover_text_missing")

    source, source_report = select_cover_source(
        output_dir,
        section_id=effective_section,
        final_path=final_path,
    )
    output = output_dir / output_name
    rendered = render_cover(source, output, text=text, fmt=fmt)
    return {
        "schema_version": SCHEMA_VERSION,
        "renderer": RENDERER_VERSION,
        "status": "pass",
        "mode": "fail_soft_local_renderer",
        "provider_calls_added": 0,
        "new_ai_stage": False,
        "new_quality_gate": False,
        "format": fmt,
        "section_id": effective_section,
        "cover_text": text,
        "source_file": source.name,
        "cover_file": output.name,
        **source_report,
        **rendered,
    }


def run_cover_lite_fail_soft(
    *,
    output_dir: Path,
    plan: Mapping[str, Any],
    fmt: str,
    final_path: Path,
    output_name: str = "cover.jpg",
    report_name: str = "cover-lite.json",
    section_id: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    base = {
        "schema_version": SCHEMA_VERSION,
        "renderer": RENDERER_VERSION,
        "mode": "fail_soft_local_renderer",
        "provider_calls_added": 0,
        "new_ai_stage": False,
        "new_quality_gate": False,
        "format": str(fmt or ""),
        "section_id": section_id,
        "cover_file": output_name,
    }
    try:
        report = build_cover_lite(
            output_dir=output_dir,
            plan=plan,
            fmt=fmt,
            final_path=final_path,
            output_name=output_name,
            section_id=section_id,
        )
    except Exception as exc:
        report = {
            **base,
            "status": "skipped_failed",
            "reason": f"{type(exc).__name__}:{str(exc)[:240]}",
        }
    atomic_write_json(output_dir / report_name, report)
    return report
