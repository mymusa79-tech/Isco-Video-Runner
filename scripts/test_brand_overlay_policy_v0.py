from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2.brand_overlay_policy import (
    BRAND_FONT_FAMILY,
    BRAND_FONT_WEIGHT,
    FORMAT_POLICIES,
    get_policy,
    opposite_text_side,
    text_box_bounds,
    unsupported_display_punctuation,
    validate_display_copy,
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

    long_p = get_policy("long")
    short_p = get_policy("short")

    assert (long_p.width, long_p.height) == (1280, 720)
    assert (short_p.width, short_p.height) == (1080, 1920)

    assert long_p.allowed_placements == ("left", "right")
    assert long_p.preferred_font_size == 84
    assert long_p.preferred_stroke_width == 3
    assert long_p.line_spacing_ratio == 0.12

    assert short_p.allowed_placements == ("top",)
    assert short_p.preferred_placement == "top"
    assert short_p.preferred_font_size == 112
    assert short_p.preferred_stroke_width == 3
    assert short_p.line_spacing_ratio == 0.10

    assert opposite_text_side("left") == "right"
    assert opposite_text_side("right") == "left"

    for fmt, p in FORMAT_POLICIES.items():
        for placement in p.allowed_placements:
            x1, y1, x2, y2 = text_box_bounds(fmt, placement)
            assert 0 <= x1 < x2 <= p.width
            assert 0 <= y1 < y2 <= p.height

    validate_overlay_request(
        fmt="long",
        placement="right",
        line_count=2,
        highlight_segments=1,
        text="لماذا يضيع وقتك دون أن تشعر؟",
    )
    validate_overlay_request(
        fmt="short",
        placement="top",
        line_count=2,
        highlight_segments=1,
        text="قلت خمس دقائق فقط",
    )

    assert unsupported_display_punctuation("قلت: خمس دقائق!") == ("!", ":")
    expect_error(lambda: validate_display_copy("قلت: خمس دقائق فقط."))

    expect_error(lambda: validate_overlay_request(
        fmt="long", placement="right", line_count=4, highlight_segments=1
    ))
    expect_error(lambda: validate_overlay_request(
        fmt="short", placement="right", line_count=2, highlight_segments=1
    ))
    expect_error(lambda: validate_overlay_request(
        fmt="short", placement="top", line_count=2, highlight_segments=2
    ))
    expect_error(lambda: validate_overlay_request(
        fmt="long", placement="left", line_count=2, highlight_segments=1,
        has_solid_black_box=True,
    ))
    expect_error(lambda: opposite_text_side("center"))  # type: ignore[arg-type]

    print("brand overlay policy v0: PASS")


if __name__ == "__main__":
    main()
