from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2.brand_overlay_policy import (
    ALLOWED_TEXT_SIDES,
    BRAND_FONT_FAMILY,
    BRAND_FONT_WEIGHT,
    FORMAT_POLICIES,
    get_policy,
    opposite_text_side,
    text_box_bounds,
    validate_overlay_request,
)


def expect_error(fn) -> None:
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def main() -> None:
    assert BRAND_FONT_FAMILY == "Noto Kufi Arabic"
    assert BRAND_FONT_WEIGHT == "Black"
    assert ALLOWED_TEXT_SIDES == ("left", "right")

    assert (get_policy("long").width, get_policy("long").height) == (1280, 720)
    assert (get_policy("short").width, get_policy("short").height) == (1080, 1920)

    assert opposite_text_side("left") == "right"
    assert opposite_text_side("right") == "left"

    for fmt, p in FORMAT_POLICIES.items():
        for side in ALLOWED_TEXT_SIDES:
            x1, y1, x2, y2 = text_box_bounds(fmt, side)
            assert 0 <= x1 < x2 <= p.width
            assert 0 <= y1 < y2 <= p.height
        validate_overlay_request(
            fmt=fmt,
            text_side="left",
            line_count=p.max_lines,
            highlight_segments=1,
        )

    expect_error(lambda: validate_overlay_request(
        fmt="long", text_side="left", line_count=4, highlight_segments=1
    ))
    expect_error(lambda: validate_overlay_request(
        fmt="short", text_side="right", line_count=2, highlight_segments=2
    ))
    expect_error(lambda: validate_overlay_request(
        fmt="long", text_side="right", line_count=2, highlight_segments=1,
        has_solid_black_box=True,
    ))
    expect_error(lambda: opposite_text_side("center"))  # type: ignore[arg-type]

    print("brand overlay policy v0: PASS")


if __name__ == "__main__":
    main()
