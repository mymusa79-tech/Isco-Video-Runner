from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TextSide = Literal["left", "right"]
TextPlacement = Literal["left", "right", "top"]
FormatName = Literal["long", "short"]

BRAND_FONT_FAMILY = "Noto Kufi Arabic"
BRAND_FONT_WEIGHT = "Black"

PRIMARY_TEXT_COLOR = "#FFFFFF"
HIGHLIGHT_TEXT_COLOR = "#D7A85B"
MAX_HIGHLIGHT_SEGMENTS = 1
ALLOW_SOLID_BLACK_TEXT_BOX = False
ALLOW_RANDOM_TEXT_PLACEMENT = False

# Noto Kufi Arabic Black intentionally remains the single display font.
# The font file lacks several Latin punctuation glyphs; V0 keeps thumbnail
# copy punctuation-light instead of silently falling back to a second font.
UNSUPPORTED_DISPLAY_PUNCTUATION = frozenset({":", ".", "!", "…", "-", "—", "«", "»"})


@dataclass(frozen=True)
class OverlayFormatPolicy:
    width: int
    height: int
    safe_left_ratio: float
    safe_right_ratio: float
    safe_top_ratio: float
    safe_bottom_ratio: float
    text_box_width_ratio: float
    max_lines: int
    font_size_min: int
    font_size_max: int
    preferred_font_size: int
    line_spacing_ratio: float
    preferred_stroke_width: int
    logo_max_width_ratio: float
    mobile_preview_width: int
    allowed_placements: tuple[TextPlacement, ...]
    preferred_placement: TextPlacement
    top_text_height_ratio: float


# Calibrated against real Cloudflare FLUX.2 probe images and a 360px mobile preview.
FORMAT_POLICIES: dict[FormatName, OverlayFormatPolicy] = {
    "long": OverlayFormatPolicy(
        width=1280,
        height=720,
        safe_left_ratio=0.07,
        safe_right_ratio=0.07,
        safe_top_ratio=0.08,
        safe_bottom_ratio=0.08,
        text_box_width_ratio=0.44,
        max_lines=3,
        font_size_min=56,
        font_size_max=96,
        preferred_font_size=84,
        line_spacing_ratio=0.12,
        preferred_stroke_width=3,
        logo_max_width_ratio=0.09,
        mobile_preview_width=360,
        allowed_placements=("left", "right"),
        preferred_placement="right",
        top_text_height_ratio=0.0,
    ),
    "short": OverlayFormatPolicy(
        width=1080,
        height=1920,
        safe_left_ratio=0.08,
        safe_right_ratio=0.08,
        safe_top_ratio=0.09,
        safe_bottom_ratio=0.18,
        text_box_width_ratio=0.84,
        max_lines=3,
        font_size_min=88,
        font_size_max=124,
        preferred_font_size=112,
        line_spacing_ratio=0.10,
        preferred_stroke_width=3,
        logo_max_width_ratio=0.14,
        mobile_preview_width=360,
        allowed_placements=("top",),
        preferred_placement="top",
        top_text_height_ratio=0.25,
    ),
}


def get_policy(fmt: FormatName) -> OverlayFormatPolicy:
    try:
        return FORMAT_POLICIES[fmt]
    except KeyError as exc:
        raise ValueError(f"unsupported overlay format: {fmt}") from exc


def opposite_text_side(subject_side: TextSide) -> TextSide:
    if subject_side == "left":
        return "right"
    if subject_side == "right":
        return "left"
    raise ValueError(f"unsupported subject side: {subject_side}")


def text_box_bounds(
    fmt: FormatName,
    placement: TextPlacement,
) -> tuple[int, int, int, int]:
    """Return deterministic x1,y1,x2,y2 bounds for the calibrated text area."""
    p = get_policy(fmt)
    if placement not in p.allowed_placements:
        raise ValueError(f"unsupported text placement for {fmt}: {placement}")

    left = round(p.width * p.safe_left_ratio)
    right = p.width - round(p.width * p.safe_right_ratio)
    top = round(p.height * p.safe_top_ratio)
    bottom = p.height - round(p.height * p.safe_bottom_ratio)

    if placement == "top":
        height = round(p.height * p.top_text_height_ratio)
        return left, top, right, min(top + height, bottom)

    box_w = round(p.width * p.text_box_width_ratio)
    if placement == "left":
        x1, x2 = left, min(left + box_w, right)
    else:
        x2, x1 = right, max(right - box_w, left)
    return x1, top, x2, bottom


def unsupported_display_punctuation(text: str) -> tuple[str, ...]:
    return tuple(sorted({ch for ch in str(text or "") if ch in UNSUPPORTED_DISPLAY_PUNCTUATION}))


def validate_display_copy(text: str) -> None:
    unsupported = unsupported_display_punctuation(text)
    if unsupported:
        joined = " ".join(repr(ch) for ch in unsupported)
        raise ValueError(f"unsupported display punctuation for Noto Kufi Arabic Black: {joined}")


def validate_overlay_request(
    *,
    fmt: FormatName,
    placement: TextPlacement,
    line_count: int,
    highlight_segments: int,
    text: str = "",
    has_solid_black_box: bool = False,
) -> None:
    p = get_policy(fmt)

    if placement not in p.allowed_placements:
        raise ValueError(f"unsupported text placement for {fmt}: {placement}")
    if line_count < 1 or line_count > p.max_lines:
        raise ValueError(f"line count must be between 1 and {p.max_lines}")
    if highlight_segments < 0 or highlight_segments > MAX_HIGHLIGHT_SEGMENTS:
        raise ValueError("only one highlighted word/phrase is allowed")
    if has_solid_black_box and not ALLOW_SOLID_BLACK_TEXT_BOX:
        raise ValueError("solid black text boxes are not allowed")
    if text:
        validate_display_copy(text)


# Rendering rules enforced by the future renderer:
# - Noto Kufi Arabic Black is the only display font in V0
# - real RTL shaping; never split an Arabic word between lines
# - long thumbnail: left/right composition; default 84px, 12% line gap, stroke 3
# - short poster: clean top composition; default 112px, 10% line gap, stroke 3
# - auto-fit only inside each format's font_size_min..font_size_max
# - primary text white; at most one gold highlight segment
# - light stroke/shadow only; no heavy sticker treatment
# - prefer a subtle gradient/vignette over a solid text rectangle
# - logo remains secondary and inside the safe area
# - arrows/circles/highlights are optional, never mandatory decoration
# - every final output must pass a 360px-wide mobile preview readability check
# - Cloudflare supplies the background only; it never owns Arabic typography,
#   logo placement, arrows, circles, or final thumbnail composition
