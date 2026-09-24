from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence
import subprocess

from .brand_overlay_policy import (
    HIGHLIGHT_TEXT_COLOR,
    PRIMARY_TEXT_COLOR,
    TextPlacement,
    FormatName,
    get_policy,
    text_box_bounds,
    validate_display_copy,
)

Role = Literal["small", "primary", "secondary"]


@dataclass(frozen=True)
class OverlayLine:
    text: str
    role: Role = "primary"
    color: str = PRIMARY_TEXT_COLOR


@dataclass(frozen=True)
class RenderResult:
    output: Path
    placement: TextPlacement
    font_path: Path
    effective_sizes: tuple[int, ...]
    soft_darkening_used: bool
    mobile_preview_width: int


def _pillow():
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageStat, features
    except Exception as exc:  # pragma: no cover - environment guard
        raise RuntimeError("brand overlay renderer requires Pillow") from exc
    if not features.check("raqm"):
        raise RuntimeError("brand overlay renderer requires Pillow RAQM for Arabic shaping")
    return Image, ImageDraw, ImageFont, ImageFilter, ImageStat


def resolve_brand_font() -> Path:
    """Resolve Noto Kufi Arabic Black from the runner instead of vendoring a font."""
    candidates: list[Path] = []
    try:
        cp = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Kufi Arabic:style=Black"],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        raw = cp.stdout.strip()
        if raw:
            candidates.append(Path(raw))
    except Exception:
        pass

    candidates.extend(
        [
            Path("/usr/share/fonts/truetype/noto/NotoKufiArabic-Black.ttf"),
            Path("/usr/share/fonts/opentype/noto/NotoKufiArabic-Black.ttf"),
        ]
    )
    for path in candidates:
        if path.is_file() and path.stat().st_size > 1024:
            return path
    raise RuntimeError("Noto Kufi Arabic Black is unavailable on this runner")


def _crop_cover(image, width: int, height: int):
    iw, ih = image.size
    target = width / height
    current = iw / ih
    if current > target:
        new_w = round(ih * target)
        left = (iw - new_w) // 2
        image = image.crop((left, 0, left + new_w, ih))
    else:
        new_h = round(iw / target)
        top = (ih - new_h) // 2
        image = image.crop((0, top, iw, top + new_h))
    return image.resize((width, height))


def _gray_stats(image, box: tuple[int, int, int, int]) -> tuple[float, float, float]:
    Image, _, _, ImageFilter, ImageStat = _pillow()
    region = image.convert("L").crop(box)
    edge = region.filter(ImageFilter.FIND_EDGES)
    mean = ImageStat.Stat(region).mean[0] / 255.0
    std = ImageStat.Stat(region).stddev[0] / 128.0
    edge_mean = ImageStat.Stat(edge).mean[0] / 255.0
    return mean, std, edge_mean


def _placement_score(image, box: tuple[int, int, int, int]) -> float:
    mean, std, edge = _gray_stats(image, box)
    darkness = 1.0 - mean
    return darkness * 1.15 - std * 0.70 - edge * 1.60


def choose_placement(image, fmt: FormatName, preferred: TextPlacement | None = None) -> TextPlacement:
    policy = get_policy(fmt)
    scored: list[tuple[float, TextPlacement]] = []
    for placement in policy.allowed_placements:
        box = text_box_bounds(fmt, placement)
        score = _placement_score(image, box)
        if preferred == placement:
            score += 0.12
        scored.append((score, placement))
    scored.sort(reverse=True)
    return scored[0][1]


def _role_size(fmt: FormatName, role: Role) -> int:
    p = get_policy(fmt)
    if role == "small":
        return p.intro_font_size
    if role == "secondary":
        return round((p.intro_font_size + p.highlight_font_size) * 0.62)
    return p.highlight_font_size


def _fit_sizes(
    draw,
    lines: Sequence[OverlayLine],
    font_path: Path,
    fmt: FormatName,
    box_width: int,
    *,
    stroke: int,
):
    _, _, ImageFont, _, _ = _pillow()
    sizes = [_role_size(fmt, line.role) for line in lines]

    def fits(scale: float):
        fonts = [
            ImageFont.truetype(
                str(font_path),
                max(get_policy(fmt).font_size_min, round(size * scale)),
                layout_engine=ImageFont.Layout.RAQM,
            )
            for size in sizes
        ]
        for line, font in zip(lines, fonts):
            bounds = draw.textbbox(
                (0, 0),
                line.text,
                font=font,
                direction="rtl",
                language="ar",
                stroke_width=stroke,
            )
            if bounds[2] - bounds[0] > box_width:
                return None
        return fonts

    for step in range(100, 49, -2):
        scale = step / 100.0
        fonts = fits(scale)
        if fonts is not None:
            return fonts, tuple(font.size for font in fonts)
    raise RuntimeError("Arabic overlay text cannot fit calibrated safe area")


def _needs_soft_darkening(image, box: tuple[int, int, int, int]) -> bool:
    mean, std, edge = _gray_stats(image, box)
    return mean > 0.48 or std > 0.82 or edge > 0.22


def _soft_darken(image, box: tuple[int, int, int, int]):
    Image, ImageDraw, _, ImageFilter, _ = _pillow()
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x1, y1, x2, y2 = box
    pad = 22
    draw.rounded_rectangle(
        (x1 - pad, y1 - pad, x2 + pad, y2 + pad),
        radius=38,
        fill=(0, 0, 0, 34),
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(44))
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def render_brand_overlay(
    background: Path,
    output: Path,
    *,
    fmt: FormatName,
    lines: Sequence[OverlayLine],
    preferred_placement: TextPlacement | None = None,
) -> RenderResult:
    if not lines or len(lines) > get_policy(fmt).max_lines:
        raise ValueError("overlay lines must contain 1..max_lines items")
    for line in lines:
        validate_display_copy(line.text)

    Image, ImageDraw, _, _, _ = _pillow()
    policy = get_policy(fmt)
    font_path = resolve_brand_font()

    image = Image.open(background).convert("RGB")
    image = _crop_cover(image, policy.width, policy.height)

    placement = choose_placement(image, fmt, preferred_placement)
    box = text_box_bounds(fmt, placement)
    softened = _needs_soft_darkening(image, box)
    if softened:
        image = _soft_darken(image, box)

    draw = ImageDraw.Draw(image)
    stroke = policy.preferred_stroke_width
    fonts, effective_sizes = _fit_sizes(
        draw,
        lines,
        font_path,
        fmt,
        box[2] - box[0],
        stroke=stroke,
    )

    line_heights: list[int] = []
    for line, font in zip(lines, fonts):
        bounds = draw.textbbox(
            (0, 0),
            line.text,
            font=font,
            direction="rtl",
            language="ar",
            stroke_width=stroke,
        )
        line_heights.append(bounds[3] - bounds[1])

    gaps = [
        max(14, round(min(effective_sizes[i], effective_sizes[i + 1]) * policy.line_spacing_ratio))
        for i in range(max(0, len(lines) - 1))
    ]
    total_height = sum(line_heights) + sum(gaps)
    x1, y1, x2, y2 = box
    y = y1 + max(0, (y2 - y1 - total_height) // 2)

    if placement == "left":
        x, anchor = x1, "la"
    elif placement == "right":
        x, anchor = x2, "ra"
    else:
        x, anchor = (x1 + x2) // 2, "ma"

    for index, (line, font, line_height) in enumerate(zip(lines, fonts, line_heights)):
        draw.text(
            (x, y),
            line.text,
            font=font,
            fill=line.color,
            anchor=anchor,
            direction="rtl",
            language="ar",
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, 175),
        )
        y += line_height
        if index < len(gaps):
            y += gaps[index]

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, quality=95)

    # Minimal mobile gate: projected smallest display size must remain readable.
    projected = min(effective_sizes) * (policy.mobile_preview_width / policy.width)
    threshold = 14.0 if fmt == "long" else 18.0
    if projected < threshold:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            f"mobile readability gate failed: projected font {projected:.1f}px < {threshold:.1f}px"
        )

    return RenderResult(
        output=output,
        placement=placement,
        font_path=font_path,
        effective_sizes=effective_sizes,
        soft_darkening_used=softened,
        mobile_preview_width=policy.mobile_preview_width,
    )


__all__ = [
    "HIGHLIGHT_TEXT_COLOR",
    "OverlayLine",
    "RenderResult",
    "render_brand_overlay",
    "resolve_brand_font",
]
