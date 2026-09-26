from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, features


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


def _fit_font(text: str, *, max_width: int, start: int, minimum: int = 48) -> ImageFont.FreeTypeFont:
    probe = ImageDraw.Draw(Image.new("L", (20, 20), 0))
    for size in range(start, minimum - 1, -2):
        font = _font(FONT_BLACK, size)
        stroke = max(3, round(size * 0.026))
        bbox = probe.textbbox(
            (0, 0), text, font=font, direction="rtl", language="ar", stroke_width=stroke
        )
        if bbox[2] - bbox[0] <= max_width:
            return font
    return _font(FONT_BLACK, minimum)


def _render_line(
    base: Image.Image,
    text: str,
    *,
    center_x: int,
    center_y: int,
    max_width: int,
    start_size: int,
    gold: bool,
) -> None:
    font = _fit_font(text, max_width=max_width, start=start_size)
    size = font.size
    stroke = max(5, round(size * 0.03))
    depth = max(7, round(size * 0.048))
    shadow_x = max(9, round(size * 0.04))
    shadow_y = max(11, round(size * 0.052))

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
        stroke_width=stroke + 3,
        stroke_fill=(0, 0, 0, 150),
    )
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(max(5, round(size * 0.018))))
    base.alpha_composite(shadow_layer)

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
    face = _gradient(
        base.size,
        GOLD_TOP if gold else WHITE_TOP,
        GOLD_BOTTOM if gold else WHITE_BOTTOM,
    )
    base.alpha_composite(
        Image.composite(face, Image.new("RGBA", base.size, (0, 0, 0, 0)), mask)
    )


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
    image = _grade(_cover_crop(source, 1080, 1920)).convert("RGBA")
    veil = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(veil)
    draw.rounded_rectangle((70, 315, 1010, 900), radius=120, fill=(0, 0, 0, 35))
    image.alpha_composite(veil.filter(ImageFilter.GaussianBlur(90)))
    return image


def _landscape_background(source: Image.Image) -> Image.Image:
    # Real Film/Podcast assets are landscape. For isolated prototype runs against
    # archived portrait Shorts, preserve the source on the left and synthesize
    # only a blurred local continuation on the right. No generative model is used.
    background = _cover_crop(source, 1280, 720).filter(ImageFilter.GaussianBlur(18))
    background = ImageEnhance.Brightness(background).enhance(0.58)
    foreground = source.convert("RGB").resize((405, 720), Image.Resampling.LANCZOS)
    canvas = background.convert("RGBA")
    canvas.alpha_composite(foreground.convert("RGBA"), (0, 0))
    veil = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(veil).rectangle((390, 0, 1280, 720), fill=(22, 16, 12, 55))
    canvas.alpha_composite(veil)
    return canvas


def render_short(source: Image.Image, output: Path, *, top: str, bottom: str) -> Path:
    image = _vertical_background(source)
    _render_line(image, top, center_x=540, center_y=555, max_width=830, start_size=300, gold=False)
    _render_line(image, bottom, center_x=540, center_y=790, max_width=850, start_size=300, gold=True)
    ImageDraw.Draw(image).line((330, 930, 760, 918), fill=(233, 172, 48, 230), width=10)
    image.convert("RGB").save(output, quality=94, subsampling=0)
    return output


def render_film(source: Image.Image, output: Path, *, top: str, bottom: str) -> Path:
    image = _landscape_background(source)
    _render_line(image, top, center_x=835, center_y=235, max_width=760, start_size=205, gold=False)
    _render_line(image, bottom, center_x=835, center_y=430, max_width=760, start_size=220, gold=True)
    ImageDraw.Draw(image).line((670, 548, 1000, 538), fill=(233, 172, 48, 220), width=8)
    image.convert("RGB").save(output, quality=94, subsampling=0)
    return output


def render_podcast(source: Image.Image, output: Path, *, top: str, bottom: str) -> Path:
    image = _landscape_background(source)
    draw = ImageDraw.Draw(image)
    tag_font = _font(FONT_BOLD, 42)
    draw.rounded_rectangle(
        (1000, 54, 1215, 118),
        radius=20,
        fill=(20, 16, 13, 190),
        outline=(215, 168, 91, 210),
        width=2,
    )
    draw.text(
        (1108, 86),
        "خارج النص",
        font=tag_font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=(245, 220, 170, 255),
    )
    _render_line(image, top, center_x=835, center_y=245, max_width=750, start_size=190, gold=False)
    _render_line(image, bottom, center_x=835, center_y=430, max_width=760, start_size=212, gold=True)
    signature = _font(FONT_MEDIUM, 30)
    draw = ImageDraw.Draw(image)
    draw.text(
        (835, 595),
        "بودكاست نداء اليقظة",
        font=signature,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=(238, 229, 214, 215),
    )
    image.convert("RGB").save(output, quality=94, subsampling=0)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated Cover Studio V2 profile prototype")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--format", choices=("short", "film", "podcast"), required=True)
    parser.add_argument("--top", default="البداية")
    parser.add_argument("--bottom", default="الصامتة")
    args = parser.parse_args()

    if not features.check_feature("raqm"):
        raise RuntimeError("Pillow Raqm support is required for Arabic Cover Studio V2")

    source = Image.open(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "short":
        render_short(source, args.output, top=args.top, bottom=args.bottom)
    elif args.format == "film":
        render_film(source, args.output, top=args.top, bottom=args.bottom)
    else:
        render_podcast(source, args.output, top=args.top, bottom=args.bottom)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
