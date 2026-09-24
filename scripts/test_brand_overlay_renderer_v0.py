from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from clean_v2.brand_overlay_policy import HIGHLIGHT_TEXT_COLOR
from clean_v2.brand_overlay_renderer import OverlayLine, render_brand_overlay, resolve_brand_font


def main() -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        raise RuntimeError("Pillow missing on runner") from exc

    root = Path("artifacts/brand-overlay-renderer-v0")
    root.mkdir(parents=True, exist_ok=True)

    # Synthetic backgrounds keep this test zero-provider and deterministic.
    long_bg = root / "long-bg.jpg"
    img = Image.new("RGB", (1280, 720), (34, 30, 27))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 590, 720), fill=(18, 24, 31))
    draw.ellipse((150, 150, 470, 620), fill=(48, 39, 32))
    img.save(long_bg, quality=90)

    short_bg = root / "short-bg.jpg"
    img = Image.new("RGB", (1080, 1920), (22, 28, 36))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 700, 1080, 1920), fill=(35, 28, 25))
    draw.ellipse((320, 650, 770, 1500), fill=(48, 41, 39))
    img.save(short_bg, quality=90)

    long_out = root / "long-final.jpg"
    long_result = render_brand_overlay(
        long_bg,
        long_out,
        fmt="long",
        lines=[
            OverlayLine("لماذا يضيع وقتك", role="small"),
            OverlayLine("دون أن تشعر", role="primary", color=HIGHLIGHT_TEXT_COLOR),
        ],
        preferred_placement="right",
    )
    assert long_out.is_file() and long_out.stat().st_size > 1024
    assert Image.open(long_out).size == (1280, 720)
    assert long_result.placement in {"left", "right"}
    assert len(long_result.effective_sizes) == 2

    short_out = root / "short-final.jpg"
    short_result = render_brand_overlay(
        short_bg,
        short_out,
        fmt="short",
        lines=[
            OverlayLine("قلت", role="small"),
            OverlayLine("خمس دقائق فقط", role="primary", color=HIGHLIGHT_TEXT_COLOR),
        ],
        preferred_placement="top",
    )
    assert short_out.is_file() and short_out.stat().st_size > 1024
    assert Image.open(short_out).size == (1080, 1920)
    assert short_result.placement == "top"
    assert len(short_result.effective_sizes) == 2

    resolved = resolve_brand_font()
    assert resolved.is_file()
    print(f"FONT={resolved}")
    print(f"LONG={long_result}")
    print(f"SHORT={short_result}")
    print("brand overlay renderer v0: PASS")


if __name__ == "__main__":
    main()
