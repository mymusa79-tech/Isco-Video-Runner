from __future__ import annotations

import argparse
import hashlib
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat, features


FONT_BLACK = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Black.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf"
FONT_MEDIUM = "/usr/share/fonts/truetype/noto/NotoKufiArabic-Medium.ttf"

WHITE_TOP = (255, 255, 255, 255)
WHITE_BOTTOM = (220, 222, 225, 255)
GOLD_TOP = (255, 232, 120, 255)
GOLD_BOTTOM = (215, 156, 42, 255)
OUTLINE = (15, 13, 10, 255)
EXTRUSION = (42, 28, 18, 255)
SHADOW = (0, 0, 0, 170)

FAMILIES = {
    "split_cinematic",
    "centered_editorial",
    "bold_impact",
    "minimal_warm",
    "stacked_story",
    "podcast_signature",
}
STYLES = {"clean", "depth", "impact"}


@dataclass(frozen=True)
class LayoutDecision:
    family: str
    style: str
    text_side: str
    reason: str


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip()


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=size, layout_engine=ImageFont.Layout.RAQM)


def _gradient(size: tuple[int, int], top: tuple[int, ...], bottom: tuple[int, ...]) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        mix = y / max(1, height - 1)
        colour = tuple(round(top[i] * (1 - mix) + bottom[i] * mix) for i in range(4))
        draw.line((0, y, width, y), fill=colour)
    return image


def _fit_font(
    text: str,
    *,
    max_width: int,
    start: int,
    minimum: int = 44,
    font_path: str = FONT_BLACK,
) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (20, 20), 0))
    for size in range(start, minimum - 1, -2):
        font = _font(font_path, size)
        stroke = max(2, round(size * 0.024))
        bbox = probe.textbbox(
            (0, 0),
            text,
            font=font,
            direction="rtl",
            language="ar",
            stroke_width=stroke,
        )
        if bbox[2] - bbox[0] <= max_width:
            return font
    return _font(font_path, minimum)


def _stable_rng(*parts: object) -> random.Random:
    seed = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).digest()
    return random.Random(int.from_bytes(seed[:8], "big"))


def _zone_detail(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    sample = image.convert("L").crop(box).resize((96, 64), Image.Resampling.BILINEAR)
    edges = sample.filter(ImageFilter.FIND_EDGES)
    edge_mean = ImageStat.Stat(edges).mean[0]
    variance = ImageStat.Stat(sample).var[0] ** 0.5
    return float(edge_mean + variance * 0.35)


def _quiet_side(image: Image.Image) -> tuple[str, float]:
    width, height = image.size
    left = _zone_detail(image, (0, 0, width // 2, height))
    right = _zone_detail(image, (width // 2, 0, width, height))
    difference = abs(left - right)
    if difference < 5.0:
        return "center", difference
    return ("left" if left < right else "right"), difference


def _mean_luma(image: Image.Image) -> float:
    return float(ImageStat.Stat(image.convert("L").resize((64, 64))).mean[0])


def choose_layout(
    source: Image.Image,
    *,
    fmt: str,
    text: str,
    forced_family: str | None = None,
    forced_style: str | None = None,
) -> LayoutDecision:
    if forced_family:
        family = forced_family
        side, delta = _quiet_side(source)
        style = forced_style or ("clean" if family == "minimal_warm" else "depth")
        return LayoutDecision(family, style, side, f"forced_family quiet_delta={delta:.1f}")

    words = _clean(text).split()
    count = len(words)
    side, delta = _quiet_side(source)
    luma = _mean_luma(source)
    rng = _stable_rng(fmt, text, round(delta, 1), round(luma, 1))

    if fmt == "podcast":
        eligible = ["podcast_signature", "split_cinematic", "centered_editorial"]
    elif count <= 2:
        eligible = ["bold_impact", "centered_editorial", "split_cinematic"]
    elif count == 3:
        eligible = ["bold_impact", "split_cinematic", "minimal_warm"]
    elif count == 4:
        eligible = ["stacked_story", "split_cinematic", "centered_editorial"]
    else:
        eligible = ["stacked_story", "minimal_warm", "split_cinematic"]

    if side != "center" and delta >= 8.0 and "split_cinematic" in eligible:
        family = "split_cinematic"
        reason = f"quiet_{side}_zone delta={delta:.1f}"
    elif luma < 72 and "minimal_warm" in eligible:
        family = "minimal_warm"
        reason = f"dark_source luma={luma:.1f}"
    else:
        family = eligible[rng.randrange(len(eligible))]
        reason = f"deterministic_tiebreak words={count} luma={luma:.1f} delta={delta:.1f}"

    if forced_style:
        style = forced_style
    elif family == "minimal_warm":
        style = "clean"
    elif family == "bold_impact":
        style = "impact"
    else:
        style = "depth"
    return LayoutDecision(family, style, side, reason)


def _text_style(style: str, size: int) -> dict[str, int | bool]:
    if style == "clean":
        return {
            "stroke": max(3, round(size * 0.018)),
            "depth": max(2, round(size * 0.012)),
            "shadow_x": max(5, round(size * 0.018)),
            "shadow_y": max(7, round(size * 0.025)),
            "blur": max(4, round(size * 0.012)),
            "texture": False,
        }
    if style == "impact":
        return {
            "stroke": max(7, round(size * 0.040)),
            "depth": max(10, round(size * 0.065)),
            "shadow_x": max(12, round(size * 0.055)),
            "shadow_y": max(15, round(size * 0.070)),
            "blur": max(6, round(size * 0.020)),
            "texture": True,
        }
    return {
        "stroke": max(5, round(size * 0.030)),
        "depth": max(7, round(size * 0.045)),
        "shadow_x": max(9, round(size * 0.040)),
        "shadow_y": max(11, round(size * 0.052)),
        "blur": max(5, round(size * 0.018)),
        "texture": False,
    }


def _texture_cutouts(mask: Image.Image, *, seed_text: str, amount: int = 24) -> Image.Image:
    result = mask.copy()
    draw = ImageDraw.Draw(result)
    rng = _stable_rng(seed_text, mask.size)
    width, height = mask.size
    for _ in range(amount):
        x = rng.randint(max(0, width // 10), max(1, width - width // 10))
        y = rng.randint(max(0, height // 5), max(1, height - height // 5))
        length = rng.randint(12, 46)
        draw.line((x, y, min(width, x + length), y + rng.randint(-3, 3)), fill=0, width=rng.randint(1, 3))
    return result


def _render_line(
    base: Image.Image,
    text: str,
    *,
    center_x: int,
    center_y: int,
    max_width: int,
    start_size: int,
    gold: bool,
    style: str,
    font_path: str = FONT_BLACK,
) -> tuple[int, int]:
    font = _fit_font(text, max_width=max_width, start=start_size, font_path=font_path)
    size = font.size
    spec = _text_style(style, size)
    stroke = int(spec["stroke"])
    depth = int(spec["depth"])
    shadow_x = int(spec["shadow_x"])
    shadow_y = int(spec["shadow_y"])

    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text(
        (center_x + shadow_x, center_y + shadow_y),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=SHADOW,
        stroke_width=stroke + 2,
        stroke_fill=(0, 0, 0, 145),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(int(spec["blur"])))
    base.alpha_composite(shadow_layer)

    draw = ImageDraw.Draw(base)
    if depth > 0:
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
        fill=(245, 245, 245, 255),
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
    if bool(spec["texture"]):
        mask = _texture_cutouts(mask, seed_text=text, amount=max(12, min(42, len(text) * 2)))

    face = _gradient(
        base.size,
        GOLD_TOP if gold else WHITE_TOP,
        GOLD_BOTTOM if gold else WHITE_BOTTOM,
    )
    base.alpha_composite(
        Image.composite(face, Image.new("RGBA", base.size, (0, 0, 0, 0)), mask)
    )
    return size, depth


def _grade(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Contrast(image).enhance(1.08)
    image = ImageEnhance.Color(image).enhance(1.06)
    warm = Image.new("RGB", image.size, (255, 184, 92))
    return Image.blend(image, warm, 0.035)


def _cover_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    image = image.convert("RGB")
    source_ratio = image.width / image.height
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


def _vertical_background(source: Image.Image) -> Image.Image:
    return _grade(_cover_crop(source, 1080, 1920)).convert("RGBA")


def _landscape_background(source: Image.Image) -> Image.Image:
    if source.width / max(1, source.height) >= 1.35:
        return _grade(_cover_crop(source, 1280, 720)).convert("RGBA")

    background = _cover_crop(source, 1280, 720).filter(ImageFilter.GaussianBlur(18))
    background = ImageEnhance.Brightness(background).enhance(0.58)
    foreground = source.convert("RGB").resize((405, 720), Image.Resampling.LANCZOS)
    canvas = background.convert("RGBA")
    canvas.alpha_composite(foreground.convert("RGBA"), (0, 0))
    return canvas


def _soft_veil(
    base: Image.Image,
    *,
    box: tuple[int, int, int, int],
    opacity: int,
    blur: int,
) -> None:
    veil = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(veil).rounded_rectangle(box, radius=max(40, blur), fill=(0, 0, 0, opacity))
    base.alpha_composite(veil.filter(ImageFilter.GaussianBlur(blur)))


def _split_words(text: str, parts: int) -> list[str]:
    words = _clean(text).split()
    if not words:
        return []
    parts = max(1, min(parts, len(words)))
    base, extra = divmod(len(words), parts)
    rows, cursor = [], 0
    for index in range(parts):
        size = base + (1 if index < extra else 0)
        rows.append(" ".join(words[cursor:cursor + size]))
        cursor += size
    return [row for row in rows if row]


def _text_center_for_side(side: str, *, width: int) -> int:
    if side == "left":
        return round(width * 0.30)
    if side == "right":
        return round(width * 0.70)
    return width // 2


def _accent_line(draw: ImageDraw.ImageDraw, *, x1: int, y: int, x2: int, thick: int) -> None:
    draw.line((x1, y, x2, y - max(2, thick // 2)), fill=(233, 172, 48, 220), width=thick)


def _render_family(
    image: Image.Image,
    *,
    fmt: str,
    text: str,
    decision: LayoutDecision,
) -> None:
    width, height = image.size
    words = _clean(text).split()
    family = decision.family
    style = decision.style
    side = decision.text_side
    draw = ImageDraw.Draw(image)

    if family == "podcast_signature":
        tag_font = _font(FONT_BOLD, 42 if width >= 1200 else 34)
        tag_x = width - 120
        tag_y = 82
        draw.rounded_rectangle(
            (tag_x - 105, tag_y - 32, tag_x + 105, tag_y + 32),
            radius=18,
            fill=(20, 16, 13, 178),
            outline=(215, 168, 91, 205),
            width=2,
        )
        draw.text(
            (tag_x, tag_y),
            "خارج النص",
            font=tag_font,
            anchor="mm",
            direction="rtl",
            language="ar",
            fill=(245, 220, 170, 255),
        )
        family = "split_cinematic" if side != "center" else "centered_editorial"

    if family == "split_cinematic":
        side = side if side != "center" else ("right" if fmt != "short" else "center")
        x = _text_center_for_side(side, width=width)
        max_width = int(width * (0.53 if fmt != "short" else 0.78))
        rows = _split_words(text, 2)
        if fmt == "short":
            y0, gap, start = 560, 230, 285
            _soft_veil(image, box=(80, 280, 1000, 940), opacity=30, blur=90)
        else:
            y0, gap, start = 250, 170, 205
            x1 = 40 if side == "left" else width // 2
            x2 = width // 2 if side == "left" else width - 40
            _soft_veil(image, box=(x1, 80, x2, 650), opacity=55, blur=75)
        for index, row in enumerate(rows):
            _render_line(
                image,
                row,
                center_x=x,
                center_y=y0 + index * gap,
                max_width=max_width,
                start_size=start + (14 if index else 0),
                gold=index == len(rows) - 1,
                style=style,
            )
        _accent_line(draw, x1=max(60, x - max_width // 4), y=y0 + len(rows) * gap - 30, x2=min(width - 60, x + max_width // 4), thick=8)
        return

    if family == "centered_editorial":
        rows = _split_words(text, 2 if len(words) <= 4 else 3)
        if fmt == "short":
            y0 = 520
            gap = 215 if len(rows) <= 2 else 175
            start = 265
            max_width = 850
            _soft_veil(image, box=(60, 250, 1020, 1040), opacity=38, blur=95)
        else:
            y0 = 215
            gap = 160
            start = 185
            max_width = 1040
            _soft_veil(image, box=(120, 80, 1160, 640), opacity=60, blur=95)
        for index, row in enumerate(rows):
            _render_line(
                image,
                row,
                center_x=width // 2,
                center_y=y0 + index * gap,
                max_width=max_width,
                start_size=start,
                gold=index == len(rows) - 1,
                style="clean" if style == "clean" else "depth",
            )
        return

    if family == "bold_impact":
        rows = _split_words(text, 2 if len(words) >= 3 else 1)
        if fmt == "short":
            x, y0, gap, max_width, start = width // 2, 560, 255, 880, 330
        else:
            side = side if side != "center" else "left"
            x = _text_center_for_side(side, width=width)
            y0, gap, max_width, start = 245, 190, 560, 250
            if side == "left":
                _soft_veil(image, box=(20, 60, 690, 665), opacity=72, blur=85)
            else:
                _soft_veil(image, box=(590, 60, 1260, 665), opacity=72, blur=85)
        for index, row in enumerate(rows):
            _render_line(
                image,
                row,
                center_x=x,
                center_y=y0 + index * gap,
                max_width=max_width,
                start_size=start,
                gold=index == len(rows) - 1,
                style="impact",
            )
        return

    if family == "minimal_warm":
        rows = _split_words(text, 2)
        if fmt == "short":
            x, y0, gap, max_width, start = width // 2, 610, 195, 820, 230
            _soft_veil(image, box=(120, 330, 960, 980), opacity=25, blur=110)
        else:
            side = side if side != "center" else "right"
            x = _text_center_for_side(side, width=width)
            y0, gap, max_width, start = 270, 155, 600, 165
            x1 = 60 if side == "left" else 630
            x2 = 650 if side == "left" else 1220
            _soft_veil(image, box=(x1, 90, x2, 620), opacity=42, blur=100)
        for index, row in enumerate(rows):
            _render_line(
                image,
                row,
                center_x=x,
                center_y=y0 + index * gap,
                max_width=max_width,
                start_size=start,
                gold=index == len(rows) - 1,
                style="clean",
                font_path=FONT_BOLD,
            )
        return

    # stacked_story
    rows = _split_words(text, 3 if len(words) >= 4 else 2)
    if fmt == "short":
        x, y0, gap, max_width, start = width // 2, 480, 195, 850, 250
        _soft_veil(image, box=(70, 240, 1010, 1050), opacity=35, blur=100)
    else:
        side = side if side != "center" else "left"
        x = _text_center_for_side(side, width=width)
        y0, gap, max_width, start = 190, 150, 575, 200
        if side == "left":
            _soft_veil(image, box=(20, 40, 690, 675), opacity=68, blur=88)
        else:
            _soft_veil(image, box=(590, 40, 1260, 675), opacity=68, blur=88)
    for index, row in enumerate(rows):
        if len(rows) == 3:
            gold = index == 1
            size = start + (28 if index == 1 else -12 if index == 2 else 0)
        else:
            gold = index == len(rows) - 1
            size = start + (18 if gold else 0)
        _render_line(
            image,
            row,
            center_x=x,
            center_y=y0 + index * gap,
            max_width=max_width,
            start_size=size,
            gold=gold,
            style="impact" if gold else "depth",
        )


def render_cover(
    source: Image.Image,
    output: Path,
    *,
    fmt: str,
    text: str,
    family: str | None = None,
    style: str | None = None,
) -> LayoutDecision:
    text = _clean(text)
    if not text:
        raise RuntimeError("cover_text_missing")
    if fmt not in {"short", "film", "podcast"}:
        raise RuntimeError(f"cover_format_unsupported:{fmt}")
    if family and family not in FAMILIES:
        raise RuntimeError(f"cover_family_unsupported:{family}")
    if style and style not in STYLES:
        raise RuntimeError(f"cover_style_unsupported:{style}")

    image = _vertical_background(source) if fmt == "short" else _landscape_background(source)
    decision = choose_layout(
        image,
        fmt=fmt,
        text=text,
        forced_family=family,
        forced_style=style,
    )
    _render_family(image, fmt=fmt, text=text, decision=decision)
    output.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output, quality=94, subsampling=0)
    return decision


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Cover Studio V2 design-grammar prototype")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--format", choices=("short", "film", "podcast"), required=True)
    parser.add_argument("--text", default="")
    parser.add_argument("--top", default="")
    parser.add_argument("--bottom", default="")
    parser.add_argument("--family", choices=sorted(FAMILIES), default=None)
    parser.add_argument("--style", choices=sorted(STYLES), default=None)
    args = parser.parse_args()

    if not features.check_feature("raqm"):
        raise RuntimeError("Pillow Raqm support is required for Arabic Cover Studio V2")

    text = _clean(args.text) or _clean(f"{args.top} {args.bottom}") or "البداية الصامتة"
    source = Image.open(args.source)
    decision = render_cover(
        source,
        args.output,
        fmt=args.format,
        text=text,
        family=args.family,
        style=args.style,
    )
    print(
        f"{args.output} family={decision.family} style={decision.style} "
        f"text_side={decision.text_side} reason={decision.reason}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
