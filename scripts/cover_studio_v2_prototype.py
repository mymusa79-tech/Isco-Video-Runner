from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, features


WIDTH = 1080
HEIGHT = 1920

WHITE_TOP = (255, 255, 255, 255)
WHITE_BOTTOM = (220, 222, 225, 255)
GOLD_TOP = (255, 231, 117, 255)
GOLD_BOTTOM = (215, 156, 42, 255)
OUTLINE = (14, 13, 11, 255)
EXTRUSION = (37, 26, 17, 255)
SHADOW = (0, 0, 0, 175)

FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-Black.ttf",
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-ExtraBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf",
)


def _font_path() -> str:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("Noto Kufi Arabic heavy font not found")


def _cover_crop(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    source_ratio = image.width / image.height
    target_ratio = WIDTH / HEIGHT
    if source_ratio > target_ratio:
        new_width = round(image.height * target_ratio)
        left = max(0, (image.width - new_width) // 2)
        image = image.crop((left, 0, left + new_width, image.height))
    else:
        new_height = round(image.width / target_ratio)
        top = max(0, (image.height - new_height) // 2)
        image = image.crop((0, top, image.width, top + new_height))
    return image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)


def _grade(image: Image.Image) -> Image.Image:
    image = ImageEnhance.Contrast(image).enhance(1.08)
    image = ImageEnhance.Color(image).enhance(1.06)
    warm = Image.new("RGB", image.size, (255, 184, 92))
    image = Image.blend(image, warm, 0.035)

    # Soft local veil only behind the headline. It is intentionally not a box.
    veil = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(veil)
    draw.rounded_rectangle((80, 300, 1000, 865), radius=120, fill=(0, 0, 0, 35))
    veil = veil.filter(ImageFilter.GaussianBlur(90))
    return Image.alpha_composite(image.convert("RGBA"), veil)


def _text_bbox(text: str, font: ImageFont.FreeTypeFont, *, stroke: int = 0):
    canvas = Image.new("L", (16, 16), 0)
    draw = ImageDraw.Draw(canvas)
    return draw.textbbox(
        (0, 0),
        text,
        font=font,
        direction="rtl",
        language="ar",
        stroke_width=stroke,
    )


def _fit_font(text: str, *, max_width: int, start: int = 300, minimum: int = 120):
    path = _font_path()
    for size in range(start, minimum - 1, -4):
        font = ImageFont.truetype(path, size=size, layout_engine=ImageFont.Layout.RAQM)
        stroke = max(5, round(size * 0.025))
        bbox = _text_bbox(text, font, stroke=stroke)
        if bbox[2] - bbox[0] <= max_width:
            return font
    return ImageFont.truetype(path, size=minimum, layout_engine=ImageFont.Layout.RAQM)


def _gradient(size: tuple[int, int], top: tuple[int, ...], bottom: tuple[int, ...]) -> Image.Image:
    image = Image.new("RGBA", size)
    pixels = image.load()
    height = max(1, size[1] - 1)
    for y in range(size[1]):
        mix = y / height
        colour = tuple(round(top[i] * (1 - mix) + bottom[i] * mix) for i in range(4))
        for x in range(size[0]):
            pixels[x, y] = colour
    return image


def _render_line(
    base: Image.Image,
    text: str,
    *,
    center_x: int,
    center_y: int,
    max_width: int,
    gold: bool,
) -> tuple[tuple[int, int, int, int], int]:
    font = _fit_font(text, max_width=max_width)
    size = font.size
    stroke = max(8, round(size * 0.035))
    depth = max(10, round(size * 0.055))
    shadow_dx = max(14, round(size * 0.060))
    shadow_dy = max(18, round(size * 0.075))

    probe = ImageDraw.Draw(Image.new("RGBA", base.size))
    bbox = probe.textbbox(
        (center_x, center_y),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        stroke_width=stroke,
    )

    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_layer)
    shadow_draw.text(
        (center_x + shadow_dx, center_y + shadow_dy),
        text,
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=SHADOW,
        stroke_width=stroke + 5,
        stroke_fill=(0, 0, 0, 165),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(7, round(size * 0.025))))
    base.alpha_composite(shadow_layer)

    draw = ImageDraw.Draw(base)
    for step in range(depth, 0, -2):
        draw.text(
            (center_x + step, center_y + step),
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
    top, bottom = (GOLD_TOP, GOLD_BOTTOM) if gold else (WHITE_TOP, WHITE_BOTTOM)
    face = _gradient(base.size, top, bottom)
    base.alpha_composite(Image.composite(face, Image.new("RGBA", base.size, (0, 0, 0, 0)), mask))
    return bbox, size


def _spark(base: Image.Image, x: int, y: int, *, flip: bool = False) -> None:
    draw = ImageDraw.Draw(base)
    sign = -1 if flip else 1
    segments = (
        ((0, 0), (28 * sign, -42)),
        ((8 * sign, 12), (50 * sign, 0)),
        ((0, 28), (28 * sign, 60)),
    )
    for start, end in segments:
        draw.line(
            ((x + start[0], y + start[1]), (x + end[0], y + end[1])),
            fill=(245, 184, 65, 235),
            width=9,
        )


def render(source: Path, output: Path, *, top_text: str, bottom_text: str) -> Path:
    if not features.check_feature("raqm"):
        raise RuntimeError("Pillow Raqm support is required for Arabic Cover Studio prototype")

    base = _grade(_cover_crop(Image.open(source)))
    top_y = 555
    _, top_size = _render_line(
        base,
        top_text,
        center_x=540,
        center_y=top_y,
        max_width=820,
        gold=False,
    )
    gap = max(18, round(top_size * 0.06))
    bottom_y = top_y + round(top_size * 0.82) + gap
    bottom_bbox, _ = _render_line(
        base,
        bottom_text,
        center_x=540,
        center_y=bottom_y,
        max_width=850,
        gold=True,
    )

    _spark(base, 150, bottom_y)
    _spark(base, 930, bottom_y, flip=True)

    draw = ImageDraw.Draw(base)
    underline_y = min(HEIGHT - 200, bottom_bbox[3] + 46)
    draw.line((330, underline_y, 760, underline_y - 12), fill=(233, 172, 48, 230), width=10)
    draw.line((405, underline_y + 15, 720, underline_y + 7), fill=(233, 172, 48, 180), width=4)

    output.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(output, quality=94, subsampling=0)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Cover Studio V2 visual prototype")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--top", default="البداية")
    parser.add_argument("--bottom", default="الصامتة")
    args = parser.parse_args()

    rendered = render(
        args.source,
        args.output,
        top_text=args.top,
        bottom_text=args.bottom,
    )
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
