from __future__ import annotations

import hashlib
import math
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .media import _run


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
FONT_BLACK = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Black.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf"
FONT_MEDIUM = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Medium.ttf"

WHITE_TOP = (255, 255, 255, 255)
WHITE_BOTTOM = (224, 226, 229, 255)
GOLD_TOP = (246, 207, 83, 255)
GOLD_BOTTOM = (205, 146, 35, 255)
OUTLINE = (17, 14, 10, 255)
EXTRUSION = (45, 31, 20, 255)
SHADOW = (0, 0, 0, 145)

PROGRAM_NAME = "خارج النص"
CHANNEL_NAME = "نداء اليقظة"
TONE_PROFILE = "deep_neutral"

_STOPWORDS = {
    "في", "من", "على", "إلى", "عن", "مع", "أن", "إن", "ثم", "أو", "بل",
    "لكن", "هذا", "هذه", "ذلك", "التي", "الذي", "ما", "لا", "لم", "لن",
    "كل", "فقط", "حين", "قبل", "بعد", "ليس", "كانت", "كان",
}


def _pil() -> tuple[Any, ...]:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat, features

    if not features.check_feature("raqm"):
        raise RuntimeError("cover_studio_raqm_unavailable")
    for font in (FONT_BLACK, FONT_BOLD, FONT_MEDIUM):
        if not Path(font).is_file():
            raise RuntimeError(f"cover_studio_font_missing:{Path(font).name}")
    return Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _extract_frame(source: Path):
    Image, *_ = _pil()
    source = Path(source)
    if source.suffix.lower() in IMAGE_SUFFIXES:
        return Image.open(source).convert("RGB")
    with tempfile.TemporaryDirectory(prefix="cover-studio-") as temporary:
        frame = Path(temporary) / "frame.jpg"
        _run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-ss", "0.500", "-i", str(source),
                "-frames:v", "1", "-q:v", "2", str(frame),
            ],
            timeout=90,
        )
        if not frame.is_file() or frame.stat().st_size <= 0:
            raise RuntimeError("cover_studio_frame_extract_failed")
        return Image.open(frame).convert("RGB")


def _zone_detail(image, box: tuple[int, int, int, int]) -> float:
    Image, _, _, ImageFilter, _, ImageStat = _pil()
    sample = image.convert("L").crop(box).resize((96, 64), Image.Resampling.BILINEAR)
    edges = sample.filter(ImageFilter.FIND_EDGES)
    edge_mean = ImageStat.Stat(edges).mean[0]
    variance = ImageStat.Stat(sample).var[0] ** 0.5
    return float(edge_mean + variance * 0.35)


def _quiet_side(image) -> tuple[str, float]:
    width, height = image.size
    top = int(height * 0.10)
    bottom = int(height * 0.62)
    left = _zone_detail(image, (0, top, width // 2, bottom))
    right = _zone_detail(image, (width // 2, top, width, bottom))
    delta = abs(left - right)
    if delta < 5.0:
        return "center", delta
    return ("left" if left < right else "right"), delta


def score_cover_candidate(path: Path, *, fmt: str) -> dict[str, Any]:
    *_, ImageStat = _pil()
    image = _extract_frame(path)
    sample = image.convert("L").resize((128, 96))
    stat = ImageStat.Stat(sample)
    luma = float(stat.mean[0])
    contrast = float(stat.stddev[0])
    side, quiet_delta = _quiet_side(image)

    # The channel target is not bright lifestyle imagery. Prefer moderate,
    # dimensional exposure with enough contrast and a usable quiet text zone.
    exposure_score = max(0.0, 24.0 - abs(luma - 96.0) * 0.22)
    contrast_score = min(18.0, contrast * 0.45)
    quiet_score = min(16.0, quiet_delta * 0.70)
    bright_penalty = max(0.0, (luma - 145.0) * 0.22)
    dark_penalty = max(0.0, (42.0 - luma) * 0.25)
    podcast_depth_bonus = 4.0 if fmt == "podcast" and luma <= 118.0 else 0.0

    visual_score = exposure_score + contrast_score + quiet_score + podcast_depth_bonus
    visual_score -= bright_penalty + dark_penalty
    return {
        "visual_score": round(visual_score, 3),
        "luma": round(luma, 3),
        "contrast": round(contrast, 3),
        "quiet_side": side,
        "quiet_delta": round(quiet_delta, 3),
        "tone_target": TONE_PROFILE,
    }


def rank_cover_candidates(
    candidates: Iterable[tuple[float, Path, Mapping[str, Any]]],
    *,
    fmt: str,
) -> tuple[Path, Mapping[str, Any], dict[str, Any]]:
    rows = list(candidates)[:3]
    if not rows:
        raise RuntimeError("cover_studio_no_candidates")

    scored: list[tuple[float, Path, Mapping[str, Any], dict[str, Any]]] = []
    for base_score, path, metadata in rows:
        metrics = score_cover_candidate(path, fmt=fmt)
        total = float(base_score) + float(metrics["visual_score"])
        scored.append((total, path, metadata, metrics))

    total, path, metadata, metrics = max(scored, key=lambda row: row[0])
    return path, metadata, {
        **metrics,
        "candidate_count_evaluated": len(scored),
        "combined_score": round(total, 3),
    }


def _font(path: str, size: int):
    _, _, _, _, ImageFont, _ = _pil()
    return ImageFont.truetype(path, size=size, layout_engine=ImageFont.Layout.RAQM)


def _fit_font(text: str, *, max_width: int, start: int, minimum: int, font_path: str):
    _, ImageDraw, _, _, _, _ = _pil()
    probe = ImageDraw.Draw(_pil()[0].new("L", (20, 20), 0))
    for size in range(start, minimum - 1, -2):
        font = _font(font_path, size)
        stroke = max(2, round(size * 0.02))
        bbox = probe.textbbox(
            (0, 0), text, font=font, direction="rtl", language="ar", stroke_width=stroke
        )
        if bbox[2] - bbox[0] <= max_width:
            return font
    return _font(font_path, minimum)


def _text_width(text: str, *, size: int = 120) -> int:
    if not text:
        return 0
    _, ImageDraw, _, _, _, _ = _pil()
    probe = ImageDraw.Draw(_pil()[0].new("L", (20, 20), 0))
    font = _font(FONT_BOLD, size)
    bbox = probe.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    return max(0, bbox[2] - bbox[0])


def _balanced_rows(text: str, *, maximum_rows: int = 2) -> list[str]:
    words = _clean(text).split()
    if not words:
        return []
    if len(words) <= 2 or maximum_rows <= 1:
        return [" ".join(words)]
    if maximum_rows >= 3 and len(words) >= 5:
        best_rows = None
        best_score = float("inf")
        for first in range(1, len(words) - 1):
            for second in range(first + 1, len(words)):
                rows = [
                    " ".join(words[:first]),
                    " ".join(words[first:second]),
                    " ".join(words[second:]),
                ]
                widths = [_text_width(row) for row in rows]
                score = max(widths) - min(widths) + max(widths) * 0.08
                if score < best_score:
                    best_score, best_rows = score, rows
        if best_rows:
            return best_rows
    best_split = 1
    best_score = float("inf")
    for split in range(1, len(words)):
        left = " ".join(words[:split])
        right = " ".join(words[split:])
        score = abs(_text_width(left) - _text_width(right))
        if score < best_score:
            best_score, best_split = score, split
    return [" ".join(words[:best_split]), " ".join(words[best_split:])]


def _focus_parts(text: str) -> tuple[str, str, str]:
    words = _clean(text).split()
    if not words:
        return "", "", ""
    index = len(words) - 1
    for candidate in range(len(words) - 1, -1, -1):
        bare = words[candidate].strip("،؛:؟?!….-")
        if bare and bare not in _STOPWORDS:
            index = candidate
            break
    return (
        " ".join(words[:index]).strip(),
        words[index].strip(),
        " ".join(words[index + 1:]).strip(),
    )


def _crop(image, width: int, height: int):
    Image, *_ = _pil()
    image = image.convert("RGB")
    source_ratio = image.width / max(1, image.height)
    target_ratio = width / height
    if source_ratio > target_ratio:
        new_width = round(image.height * target_ratio)
        left = max(0, (image.width - new_width) // 2)
        image = image.crop((left, 0, left + new_width, image.height))
    else:
        new_height = round(image.width / target_ratio)
        top = max(0, (image.height - new_height) // 2)
        image = image.crop((0, top, image.width, top + new_height))
    return image.resize((width, height), Image.Resampling.LANCZOS)


def _vignette(image):
    Image, ImageDraw, _, ImageFilter, _, _ = _pil()
    width, height = image.size
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    margin_x = int(width * 0.10)
    margin_y = int(height * 0.08)
    draw.ellipse(
        (-margin_x, -margin_y, width + margin_x, height + margin_y),
        fill=210,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(max(80, int(min(width, height) * 0.16))))
    shade = Image.new("RGBA", image.size, (0, 0, 0, 82))
    return Image.composite(image, shade, mask)


def _deep_grade(image):
    Image, _, ImageEnhance, _, _, ImageStat = _pil()
    image = ImageEnhance.Contrast(image.convert("RGB")).enhance(1.08)
    image = ImageEnhance.Color(image).enhance(0.94)
    mean = float(ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])
    brightness = 0.86 if mean > 145 else 0.90 if mean > 118 else 0.95
    image = ImageEnhance.Brightness(image).enhance(brightness)
    warm = Image.new("RGB", image.size, (212, 145, 76))
    image = Image.blend(image, warm, 0.018)
    return _vignette(image.convert("RGBA"))


def _prepare_canvas(source: Path, *, fmt: str):
    Image, ImageDraw, _, ImageFilter, _, _ = _pil()
    frame = _extract_frame(source)
    if fmt == "short":
        return _deep_grade(_crop(frame, 1080, 1920))

    if frame.width / max(1, frame.height) >= 1.35:
        return _deep_grade(_crop(frame, 1280, 720))

    background = _deep_grade(_crop(frame, 1280, 720)).filter(ImageFilter.GaussianBlur(15))
    foreground = _deep_grade(frame.resize((520, 720), Image.Resampling.LANCZOS))
    mask = Image.new("L", (520, 720), 255)
    mask_draw = ImageDraw.Draw(mask)
    for x in range(390, 520):
        mask_draw.line((x, 0, x, 720), fill=max(0, round(255 * (1 - (x - 390) / 130))))
    layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    layer.paste(foreground, (0, 0), mask)
    return Image.alpha_composite(background, layer)


def _gradient(size: tuple[int, int], top: tuple[int, ...], bottom: tuple[int, ...]):
    Image, ImageDraw, *_ = _pil()
    width, height = size
    result = Image.new("RGBA", size)
    draw = ImageDraw.Draw(result)
    for y in range(height):
        mix = y / max(1, height - 1)
        colour = tuple(round(top[i] * (1 - mix) + bottom[i] * mix) for i in range(4))
        draw.line((0, y, width, y), fill=colour)
    return result


def _render_line(
    base,
    text: str,
    *,
    center_x: int,
    center_y: int,
    max_width: int,
    start_size: int,
    gold: bool,
    style: str,
    font_path: str = FONT_BLACK,
) -> None:
    Image, ImageDraw, _, ImageFilter, _, _ = _pil()
    font = _fit_font(
        text, max_width=max_width, start=start_size, minimum=42, font_path=font_path
    )
    size = font.size
    if style == "clean":
        stroke, depth, sx, sy, blur = (
            max(2, round(size * 0.012)),
            max(1, round(size * 0.007)),
            max(4, round(size * 0.012)),
            max(5, round(size * 0.018)),
            max(4, round(size * 0.012)),
        )
    elif style == "impact":
        stroke, depth, sx, sy, blur = (
            max(5, round(size * 0.030)),
            max(7, round(size * 0.045)),
            max(8, round(size * 0.032)),
            max(10, round(size * 0.042)),
            max(5, round(size * 0.017)),
        )
    else:
        stroke, depth, sx, sy, blur = (
            max(4, round(size * 0.022)),
            max(4, round(size * 0.026)),
            max(6, round(size * 0.024)),
            max(8, round(size * 0.032)),
            max(4, round(size * 0.014)),
        )

    shadow = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.text(
        (center_x + sx, center_y + sy),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=SHADOW,
        stroke_width=stroke + 2,
        stroke_fill=(0, 0, 0, 125),
    )
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))

    draw = ImageDraw.Draw(base)
    for offset in range(depth, 0, -2):
        draw.text(
            (center_x + offset, center_y + offset),
            text,
            font=font,
            anchor="mm",
            direction="rtl",
            language="ar",
            fill=EXTRUSION,
            stroke_width=stroke,
            stroke_fill=OUTLINE,
        )
    draw.text(
        (center_x, center_y),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=(246, 246, 246, 255),
        stroke_width=stroke,
        stroke_fill=OUTLINE,
    )

    mask = Image.new("L", base.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.text(
        (center_x, center_y),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=255,
    )
    face = _gradient(
        base.size,
        GOLD_TOP if gold else WHITE_TOP,
        GOLD_BOTTOM if gold else WHITE_BOTTOM,
    )
    base.alpha_composite(
        Image.composite(face, Image.new("RGBA", base.size, (0, 0, 0, 0)), mask)
    )


def _stable_choice(values: list[str], *, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return values[int.from_bytes(digest[:4], "big") % len(values)]


def _choose_layout(image, *, fmt: str, text: str) -> tuple[str, str]:
    words = _clean(text).split()
    side, delta = _quiet_side(image)
    if fmt == "podcast":
        return ("podcast_split" if side != "center" else "podcast_centered"), side
    if len(words) <= 2:
        return "impact", side
    if len(words) == 3:
        return ("split" if side != "center" and delta >= 7 else "impact"), side
    eligible = ["split", "stacked"] if side != "center" else ["centered", "stacked"]
    return _stable_choice(eligible, key=f"{fmt}|{text}|{side}"), side


def render_cover_studio(
    source: Path,
    destination: Path,
    *,
    text: str,
    fmt: str,
) -> dict[str, Any]:
    Image, ImageDraw, *_ = _pil()
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas = _prepare_canvas(Path(source), fmt=fmt)
    width, height = canvas.size
    layout, side = _choose_layout(canvas, fmt=fmt, text=text)
    prefix, focus, suffix = _focus_parts(text)
    supporting = " ".join(part for part in (prefix, suffix) if part).strip()
    draw = ImageDraw.Draw(canvas)

    if fmt == "short":
        x = width // 2
        if layout == "centered":
            rows = _balanced_rows(text, maximum_rows=2)
            for index, row in enumerate(rows):
                _render_line(
                    canvas, row, center_x=x, center_y=520 + index * 190,
                    max_width=800, start_size=225, gold=index == len(rows) - 1,
                    style="clean" if len(rows) > 1 else "depth", font_path=FONT_BOLD,
                )
        elif layout == "impact":
            if supporting:
                _render_line(
                    canvas, supporting, center_x=x, center_y=500, max_width=800,
                    start_size=168, gold=False, style="depth",
                )
            _render_line(
                canvas, focus or text, center_x=x, center_y=750, max_width=800,
                start_size=290, gold=True, style="impact",
            )
        else:
            rows = _balanced_rows(text, maximum_rows=2)
            for index, row in enumerate(rows):
                _render_line(
                    canvas, row, center_x=x, center_y=500 + index * 205,
                    max_width=800, start_size=215, gold=index == len(rows) - 1,
                    style="depth",
                )
    else:
        if side == "left":
            x = int(width * 0.29)
        elif side == "right":
            x = int(width * 0.71)
        else:
            x = int(width * 0.70) if fmt == "podcast" else width // 2

        if layout in {"podcast_split", "podcast_centered"}:
            tag_font = _font(FONT_BOLD, 38)
            draw.rounded_rectangle(
                (width - 230, 46, width - 50, 104),
                radius=16,
                fill=(18, 14, 12, 170),
                outline=(207, 153, 60, 205),
                width=2,
            )
            draw.text(
                (width - 140, 75), PROGRAM_NAME,
                font=tag_font, anchor="mm", direction="rtl", language="ar",
                fill=(240, 213, 164, 255),
            )
            rows = _balanced_rows(text, maximum_rows=2)
            for index, row in enumerate(rows):
                _render_line(
                    canvas, row, center_x=x, center_y=235 + index * 170,
                    max_width=560, start_size=170 + (15 if index else 0),
                    gold=index == len(rows) - 1, style="depth",
                )
            footer_font = _font(FONT_MEDIUM, 28)
            draw.text(
                (x, 610), CHANNEL_NAME,
                font=footer_font, anchor="mm", direction="rtl", language="ar",
                fill=(230, 222, 210, 215),
            )
        elif layout == "impact":
            if supporting:
                _render_line(
                    canvas, supporting, center_x=x, center_y=210, max_width=520,
                    start_size=118, gold=False, style="depth",
                )
            _render_line(
                canvas, focus or text, center_x=x, center_y=420, max_width=520,
                start_size=230, gold=True, style="impact",
            )
        elif layout == "stacked":
            if supporting:
                rows = _balanced_rows(supporting, maximum_rows=2)
                for index, row in enumerate(rows):
                    _render_line(
                        canvas, row, center_x=x, center_y=175 + index * 105,
                        max_width=520, start_size=108, gold=False, style="depth",
                    )
            _render_line(
                canvas, focus or text, center_x=x, center_y=435, max_width=520,
                start_size=220, gold=True, style="impact",
            )
        else:
            rows = _balanced_rows(text, maximum_rows=2)
            for index, row in enumerate(rows):
                _render_line(
                    canvas, row, center_x=x, center_y=235 + index * 165,
                    max_width=540, start_size=160, gold=index == len(rows) - 1,
                    style="depth",
                )

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(destination, quality=94, subsampling=0)
    return {
        "renderer": "cover_studio_v2",
        "profile": fmt,
        "layout_family": layout,
        "text_side": side,
        "tone_profile": TONE_PROFILE,
        "podcast_program": PROGRAM_NAME if fmt == "podcast" else None,
        "channel_name": CHANNEL_NAME if fmt == "podcast" else None,
        "width": width,
        "height": height,
    }
