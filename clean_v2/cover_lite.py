from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .contracts import atomic_write_json
from .media import _run


SCHEMA_VERSION = 1
RENDERER_VERSION = "clean-v2-cover-lite-v1"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
PROFILES = {
    "short": (1080, 1920, 132, 540, 420, "vertical"),
    "film": (1280, 720, 78, 640, 205, "landscape"),
    "podcast": (1280, 720, 74, 640, 205, "podcast_landscape"),
}
WHITE = "&H00FFFFFF"
GOLD = "&H005BA8D7"
EDGE = "&H00120F0B"
DEPTH = "&H00231A12"
SHADOW = "&HA8000000"
_STOPWORDS = {
    "في", "من", "على", "إلى", "عن", "مع", "أن", "إن", "ثم", "أو", "بل",
    "لكن", "هذا", "هذه", "ذلك", "التي", "الذي", "ما", "لا", "لم", "لن", "كل", "فقط",
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _fallback(value: object) -> str:
    words = _clean(value).split()
    return " ".join(words[:5]).rstrip("،؛:.-!?؟") if words else ""


def resolve_cover_text(plan: Mapping[str, Any], *, section_id: str | None = None) -> str:
    sections = [row for row in (plan.get("sections") or []) if isinstance(row, Mapping)]
    if section_id:
        for row in sections:
            if str(row.get("id") or "") == str(section_id):
                return _clean(row.get("cover_text")) or _fallback(row.get("heading"))
    return (
        _clean(plan.get("cover_text"))
        or (_clean(sections[0].get("cover_text")) if sections else "")
        or _fallback(plan.get("title"))
        or _fallback(plan.get("promise"))
    )


def _rights(output_dir: Path) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads((Path(output_dir) / "rights-manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        row for row in (payload.get("assets") or [])
        if isinstance(row, Mapping)
    ] if isinstance(payload, Mapping) else []


def select_cover_source(
    output_dir: Path,
    *,
    section_id: str | None = None,
    final_path: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    ranked: list[tuple[int, Path, Mapping[str, Any]]] = []
    for index, row in enumerate(_rights(output_dir)):
        local_file = Path(str(row.get("local_file") or "")).name
        path = Path(output_dir) / "visuals" / local_file
        if not local_file or not path.is_file() or path.stat().st_size <= 0:
            continue
        score = max(0, 20 - index)
        score += 100 if section_id and str(row.get("section_id") or "") == str(section_id) else 0
        score += 50 if str(row.get("role") or "") == "hook" else 0
        score += 20 if str(row.get("source_actual") or "") == "ai_still" else 0
        score += 10 if not row.get("pacing_auxiliary") else 0
        ranked.append((score, path, row))
    if ranked:
        score, path, row = max(ranked, key=lambda item: item[0])
        return path, {
            "selection": "existing_visual_asset",
            "score": score,
            "section_id": str(row.get("section_id") or ""),
            "beat_id": str(row.get("beat_id") or ""),
            "role": str(row.get("role") or ""),
            "source_actual": str(row.get("source_actual") or ""),
        }

    fallback = Path(final_path) if final_path is not None else Path(output_dir) / "final.mp4"
    if fallback.is_file() and fallback.stat().st_size > 0:
        return fallback, {"selection": "final_video_frame"}
    raise RuntimeError("cover_source_missing")


def _dimensions(path: Path) -> tuple[int, int]:
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path),
        ],
        timeout=60,
    )
    match = re.fullmatch(r"(\d+)x(\d+)", result.stdout.strip())
    if not match:
        raise RuntimeError("cover_source_dimensions_invalid")
    return int(match.group(1)), int(match.group(2))


def _escape(text: str) -> str:
    return _clean(text).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def _rows(text: str) -> list[list[str]]:
    words = _clean(text).split()
    if len(words) <= 3:
        return [words]
    split = (len(words) + 1) // 2
    return [words[:split], words[split:]]


def _focus_index(words: list[str]) -> int:
    candidates: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        bare = re.sub(r"[^\w\u0600-\u06FF]+", "", word, flags=re.UNICODE)
        if bare and bare not in _STOPWORDS:
            candidates.append((len(bare), index))
    return max(candidates)[1] if candidates else max(0, len(words) - 1)


def _ass_text(text: str, *, gold: bool) -> str:
    words = _clean(text).split()
    focus = _focus_index(words)
    rendered = []
    for index, word in enumerate(words):
        value = _escape(word)
        if gold and index == focus:
            value = "{\\c" + GOLD + "}" + value + "{\\c" + WHITE + "}"
        rendered.append(value)
    split_rows = _rows(" ".join(words))
    out, cursor = [], 0
    for row in split_rows:
        count = len(row)
        out.append(r"\h\h".join(rendered[cursor:cursor + count]))
        cursor += count
    return "\u202B" + r"\N".join(out) + "\u202C"


def _write_ass(path: Path, *, text: str, fmt: str) -> tuple[int, int, str]:
    if fmt not in PROFILES:
        raise RuntimeError(f"cover_format_unsupported:{fmt}")
    width, height, font, x, y, profile = PROFILES[fmt]
    plain = _ass_text(text, gold=False)
    face = _ass_text(text, gold=True)
    path.write_text(
        f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Shadow,Noto Sans Arabic,{font},{WHITE},{WHITE},{SHADOW},{SHADOW},-1,0,0,0,100,100,0,0,1,0,0,5,40,40,0,1
Style: Depth,Noto Sans Arabic,{font},{DEPTH},{DEPTH},{DEPTH},{DEPTH},-1,0,0,0,100,100,0,0,1,2,0,5,40,40,0,1
Style: Face,Noto Sans Arabic,{font},{WHITE},{WHITE},{EDGE},{EDGE},-1,0,0,0,100,100,0,0,1,3,0,5,40,40,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
Dialogue: 0,0:00:00.00,0:00:30.00,Shadow,,0,0,0,,{{\\pos({x+7},{y+8})}}{plain}
Dialogue: 1,0:00:00.00,0:00:30.00,Depth,,0,0,0,,{{\\pos({x+3},{y+3})}}{plain}
Dialogue: 2,0:00:00.00,0:00:30.00,Face,,0,0,0,,{{\\pos({x},{y})}}{face}
""",
        encoding="utf-8",
    )
    return width, height, profile


def _subtitle_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def render_cover(source: Path, destination: Path, *, text: str, fmt: str) -> dict[str, Any]:
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ass = destination.with_name(f".{destination.stem}.cover-lite.ass")
    width, height, profile = _write_ass(ass, text=text, fmt=fmt)
    source_width, source_height = _dimensions(source)
    subtitle = f"subtitles='{_subtitle_path(ass)}'"

    if fmt == "short" and source_width >= source_height:
        filters = (
            "[0:v]split=2[bg0][fg0];"
            f"[bg0]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=20:2[bg];"
            f"[fg0]scale={width - 40}:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,"
            f"eq=contrast=1.05:saturation=0.94,{subtitle}[v]"
        )
    else:
        filters = (
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},eq=contrast=1.05:saturation=0.94,{subtitle}[v]"
        )

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if source.suffix.lower() not in IMAGE_SUFFIXES:
        command += ["-ss", "0.500"]
    command += [
        "-i", str(source), "-filter_complex", filters, "-map", "[v]",
        "-frames:v", "1", "-q:v", "2", str(destination),
    ]
    try:
        _run(command, timeout=180)
    finally:
        ass.unlink(missing_ok=True)
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("cover_output_missing")
    return {
        "width": width,
        "height": height,
        "profile": profile,
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
    if fmt == "short" and section_id is None:
        sections = [row for row in (plan.get("sections") or []) if isinstance(row, Mapping)]
        section_id = str(sections[0].get("id") or "") if sections else None
    text = resolve_cover_text(plan, section_id=section_id)
    if not text:
        raise RuntimeError("cover_text_missing")
    source, selection = select_cover_source(
        output_dir, section_id=section_id, final_path=final_path
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
        "section_id": section_id,
        "cover_text": text,
        "source_file": source.name,
        "cover_file": output.name,
        **selection,
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
            "schema_version": SCHEMA_VERSION,
            "renderer": RENDERER_VERSION,
            "status": "skipped_failed",
            "mode": "fail_soft_local_renderer",
            "provider_calls_added": 0,
            "new_ai_stage": False,
            "new_quality_gate": False,
            "format": fmt,
            "section_id": section_id,
            "cover_file": output_name,
            "reason": f"{type(exc).__name__}:{str(exc)[:240]}",
        }
    atomic_write_json(Path(output_dir) / report_name, report)
    return report
