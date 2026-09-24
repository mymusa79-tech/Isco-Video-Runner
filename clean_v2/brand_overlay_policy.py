from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TextSide = Literal["left", "right"]
FormatName = Literal["long", "short"]

BRAND_FONT_FAMILY = "Noto Kufi Arabic"
BRAND_FONT_WEIGHT = "Black"

PRIMARY_TEXT_COLOR = "#FFFFFF"
HIGHLIGHT_TEXT_COLOR = "#D7A85B"
MAX_HIGHLIGHT_SEGMENTS = 1
ALLOW_SOLID_BLACK_TEXT_BOX = False
ALLOW_RANDOM_TEXT_PLACEMENT = False
ALLOWED_TEXT_SIDES: tuple[TextSide, TextSide] = ("left", "right")

# Provisional V0 calibration values.
# These are layout bounds, not final visual tuning. The renderer may auto-fit
# within these ranges but must not exceed them without a new calibration.
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
    line_spacing_ratio: float
    logo_max_width_ratio: float
    mobile_preview_width: int


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
        line_spacing_ratio=0.12,
        logo_max_width_ratio=0.09,
        mobile_preview_width=360,
    ),
    "short": OverlayFormatPolicy(
        width=1080,
        height=1920,
        safe_left_ratio=0.08,
        safe_right_ratio=0.08,
        safe_top_ratio=0.10,
        safe_bottom_ratio=0.18,
        text_box_width_ratio=0.76,
        max_lines=3,
        font_size_min=72,
        font_size_max=132,
        line_spacing_ratio=0.12,
        logo_max_width_ratio=0.14,
        mobile_preview_width=360,
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


def text_box_bounds(fmt: FormatName, side: TextSide) -> tuple[int, int, int, int]:
    """Return deterministic x1,y1,x2,y2 bounds for the text safe area."""
    p = get_policy(fmt)
    if side not in ALLOWED_TEXT_SIDES:
        raise ValueError(f"unsupported text side: {side}")

    left = round(p.width * p.safe_left_ratio)
    right = p.width - round(p.width * p.safe_right_ratio)
    top = round(p.height * p.safe_top_ratio)
    bottom = p.height - round(p.height * p.safe_bottom_ratio)
    box_w = round(p.width * p.text_box_width_ratio)

    if side == "left":
        x1, x2 = left, min(left + box_w, right)
    else:
        x2, x1 = right, max(right - box_w, left)
    return x1, top, x2, bottom


def validate_overlay_request(
    *,
    fmt: FormatName,
    text_side: TextSide,
    line_count: int,
    highlight_segments: int,
    has_solid_black_box: bool = False,
) -> None:
    p = get_policy(fmt)

    if text_side not in ALLOWED_TEXT_SIDES:
        raise ValueError("text side must be left or right")
    if line_count < 1 or line_count > p.max_lines:
        raise ValueError(f"line count must be between 1 and {p.max_lines}")
    if highlight_segments < 0 or highlight_segments > MAX_HIGHLIGHT_SEGMENTS:
        raise ValueError("only one highlighted word/phrase is allowed")
    if has_solid_black_box and not ALLOW_SOLID_BLACK_TEXT_BOX:
        raise ValueError("solid black text boxes are not allowed")


# Rendering rules enforced by the future renderer:
# - real RTL shaping; never split an Arabic word between lines
# - auto-fit only inside font_size_min..font_size_max
# - primary text white; at most one gold highlight segment
# - light stroke/shadow only; no heavy sticker treatment
# - prefer a subtle gradient/vignette over a solid text rectangle
# - logo remains secondary and inside the safe area
# - arrows/circles/highlights are optional, never mandatory decoration
# - every final output must pass a 360px-wide mobile preview readability check
# - Cloudflare supplies the background only; it never owns Arabic typography,
#   logo placement, arrows, circles, or final thumbnail composition
