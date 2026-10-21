"""Explainer "board" scene for the approved Film / Podcast ZIP.

One still board (clean paper or dark slate) that fills up item by item while the narrator
speaks: each item is a picture the author supplied (cut out and given a sticker outline so
every board looks like one family), a Cairo label that the client renders itself (so the
Arabic is always correct), and an arrow or line that draws toward it. The author only chooses
the layout, the items and the literal sentence on which each one appears.

This module is pure rendering. It never calls a provider, a model or the network. Any failure
here is caught by the caller, which keeps the approved still image as the scene.
"""
from __future__ import annotations

import math
import os
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

WIDTH, HEIGHT, FPS = 1920, 1080, 30
LAYOUTS = {"row": (2, 5), "steps": (2, 5), "grid": (3, 8), "hub": (3, 7), "compare": (2, 2)}
# Podcast keeps its dialogue caption card over y=687..965, so its boards live in the upper part
# only; Film's sparse key text is suppressed while a board is on screen (see blocked_windows).
PODCAST_LAYOUTS = {"row": (2, 5), "steps": (2, 4), "grid": (3, 6), "compare": (2, 2)}
# Nidaa Short boards are portrait: vertical steps or a 2-column grid, kept above the static caption
# (centred at y=1260) and clear of the platform's top and bottom interface.
SHORT_LAYOUTS = {"steps": (2, 4), "grid": (3, 4)}
_LANDSCAPE = {"width": 1920, "height": 1080, "left_edge": 120, "right_edge": 1800, "title_w": 1250, "steps_w": 1500, "dots_y": 44}
PROFILES = {
    "film": {**_LANDSCAPE, "title_y": 100, "title_size": 76, "bar_y": 150, "top": 250, "top_plain": 110, "bottom": 1010},
    "podcast": {**_LANDSCAPE, "title_y": 60, "title_size": 50, "bar_y": 94, "top": 150, "top_plain": 50, "bottom": 650},
    "short": {"width": 1080, "height": 1920, "left_edge": 80, "right_edge": 1000, "title_w": 780, "steps_w": 920, "dots_y": 200,
              "title_y": 270, "title_size": 62, "bar_y": 322, "top": 380, "top_plain": 240, "bottom": 1060},
}
MAX_ITEMS = 8
MAX_TITLE_CHARS = 40
MAX_LABEL_CHARS = 30
BOARD_MAX_SCENES = {"film": 14, "podcast": 8, "short": 2}

_PALETTE = {
    # Every theme shares one modern look: depth (glow, dot grid, vignette), gradient glass cards with a
    # luminous edge, big numerals and an accent bar. Only the palette differs.
    "editorial": {  # ink and gold: the channel's default look
        "bg_top": (16, 22, 36), "bg_bot": (8, 11, 20), "grain": 1.4, "vignette": 0.40,
        "dot_grid": (255, 255, 255, 15), "glow": 0.40,
        "blobs": [((1560, 130), 560, (214, 178, 112), 0.22), ((240, 980), 640, (38, 128, 140), 0.22),
                  ((980, 1120), 520, (118, 96, 204), 0.12)],
        "ink": (248, 245, 237),
        "card_top": (46, 59, 76, 242), "card_bot": (25, 33, 47, 242), "card_border": (255, 255, 255, 58),
        "shadow": (0, 0, 0, 135), "plate_alpha": 46, "dot_off": (150, 165, 176, 72),
        "accents": [(222, 186, 118), (98, 204, 196), (240, 152, 116), (172, 164, 244)],
    },
    "slate": {  # midnight violet
        "bg_top": (10, 14, 30), "bg_bot": (22, 26, 60), "grain": 2.0, "vignette": 0.34,
        "dot_grid": (255, 255, 255, 14), "glow": 0.55,
        "blobs": [((1560, 170), 540, (96, 80, 255), 0.42), ((260, 930), 580, (0, 194, 168), 0.25), ((1120, 1010), 500, (255, 77, 141), 0.18)],
        "ink": (247, 248, 253),
        "card_top": (255, 255, 255, 40), "card_bot": (255, 255, 255, 14), "card_border": (255, 255, 255, 78),
        "shadow": (0, 0, 0, 150), "plate_alpha": 54, "dot_off": (255, 255, 255, 56),
        "accents": [(132, 116, 255), (0, 214, 182), (255, 128, 96), (255, 204, 92)],
    },
    "paper": {  # soft light
        "bg_top": (247, 248, 252), "bg_bot": (230, 234, 247), "grain": 1.6, "vignette": 0.07,
        "dot_grid": (40, 50, 110, 20), "glow": 0.50,
        "blobs": [((1560, 150), 540, (186, 168, 255), 0.60), ((250, 950), 580, (150, 240, 218), 0.55), ((1150, 1050), 520, (255, 198, 176), 0.50)],
        "ink": (18, 24, 50),
        "card_top": (255, 255, 255, 236), "card_bot": (255, 255, 255, 190), "card_border": (255, 255, 255, 255),
        "shadow": (44, 54, 120, 80), "plate_alpha": 46, "dot_off": (18, 24, 50, 40),
        "accents": [(88, 72, 244), (0, 160, 136), (244, 98, 54), (214, 146, 0)],
    },
}
THEMES = tuple(_PALETTE)

REVEAL_FIRST = 0.95       # the title gets the first moment
REVEAL_SPACING = 0.7      # never two items closer than this unless the clip is very short
REVEAL_TAIL = 2.0         # the last item is complete this long before the clip ends
LEAD_SECONDS = 0.18       # an item starts appearing a hair before its word is said
POP_SECONDS = 0.55
SETTLE_SECONDS = 1.7      # glow on the newest item fades out, then it joins the base


class BoardError(RuntimeError):
    pass


# --------------------------------------------------------------------------- fonts / text
@lru_cache(maxsize=1)
def _font_path() -> str:
    try:
        out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", "Cairo:weight=bold"],
                             capture_output=True, text=True, timeout=10, check=True).stdout
        family, _, path = out.partition("|")
        if "cairo" in family.lower() and Path(path).is_file():
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    # Cairo is the channel font. Never draw a board in another face on a production run: raising here
    # keeps the approved still image instead. Local previews and tests may opt into a stand-in.
    if os.environ.get("BOARD_ALLOW_FALLBACK_FONT") == "1":
        for candidate in ("/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
                          "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"):
            if Path(candidate).is_file():
                return candidate
        # Test/preview opt-in only: runner images may lack the exact Noto paths.
        # Production (no BOARD_ALLOW_FALLBACK_FONT=1) still fails closed without Cairo.
        try:
            out = subprocess.run(["fc-match", "-f", "%{file}", "sans-serif"],
                                 capture_output=True, text=True, timeout=10, check=True).stdout.strip()
            if Path(out).is_file():
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    raise BoardError("board_font_cairo_missing")


@lru_cache(maxsize=64)
def _font(size: int):
    from PIL import ImageFont

    return ImageFont.truetype(_font_path(), size, layout_engine=ImageFont.Layout.RAQM)


def _text_size(text: str, font) -> tuple[int, int]:
    box = font.getbbox(text, direction="rtl", language="ar")
    return box[2] - box[0], box[3] - box[1]


def _fit_label(text: str, max_width: int, max_size: int, min_size: int = 30, max_lines: int = 2):
    """Largest size that fits on one line, else the largest that fits on two balanced lines."""
    words = text.split()
    for size in range(max_size, min_size - 1, -2):
        if _text_size(text, _font(size))[0] <= max_width:
            return size, [text]
    if max_lines >= 2 and len(words) >= 2:
        best = None
        for cut in range(1, len(words)):
            lines = [" ".join(words[:cut]), " ".join(words[cut:])]
            for size in range(max_size, min_size - 1, -2):
                widest = max(_text_size(line, _font(size))[0] for line in lines)
                if widest <= max_width:
                    score = (size, -abs(len(lines[0]) - len(lines[1])))
                    if best is None or score > best[0]:
                        best = (score, size, lines)
                    break
        if best:
            return best[1], best[2]
    return min_size, [text]


def _draw_lines(draw, lines: Sequence[str], centre: tuple[float, float], size: int, fill, gap: float = 1.18):
    font = _font(size)
    total = (len(lines) - 1) * size * gap
    y = centre[1] - total / 2
    for line in lines:
        draw.text((centre[0], y), line, font=font, fill=fill, anchor="mm", direction="rtl", language="ar")
        y += size * gap


# --------------------------------------------------------------------------- easing
def _out_cubic(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


def _out_back(p: float) -> float:
    p = min(1.0, max(0.0, p))
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * (p - 1) ** 3 + c1 * (p - 1) ** 2


def _cutout(image):
    """RGBA with a real alpha. Opaque pictures on a plain backdrop are keyed out; others stay a card."""
    from PIL import Image, ImageDraw, ImageFilter

    image = image.convert("RGBA")
    alpha = image.getchannel("A")
    if alpha.getextrema()[0] < 250:
        return image, True
    width, height = image.size
    rgb = image.convert("RGB")
    probes = [rgb.getpixel(p) for p in ((2, 2), (width - 3, 2), (2, height - 3), (width - 3, height - 3),
                                         (width // 2, 2), (width // 2, height - 3))]
    base = tuple(sorted(c[i] for c in probes)[len(probes) // 2] for i in range(3))
    if max(max(abs(c[i] - base[i]) for i in range(3)) for c in probes) > 26:
        return image, False  # not a plain backdrop: a photo, keep it whole
    small = rgb.resize((max(64, width // 4), max(64, height // 4)))
    sw, sh = small.size
    distance = Image.new("L", (sw, sh))
    px = small.load()
    dp = distance.load()
    for y in range(sh):
        for x in range(sw):
            r, g, b = px[x, y]
            dp[x, y] = 255 if max(abs(r - base[0]), abs(g - base[1]), abs(b - base[2])) <= 42 else 0
    # background = plain-colour pixels connected to the border (so a white shirt stays)
    flood = distance.copy()
    for corner in ((0, 0), (sw - 1, 0), (0, sh - 1), (sw - 1, sh - 1), (sw // 2, 0), (sw // 2, sh - 1)):
        if flood.getpixel(corner) == 255:
            ImageDraw.floodfill(flood, corner, 128)
    background = flood.point(lambda v: 255 if v == 128 else 0)
    subject = background.point(lambda v: 0 if v else 255).resize((width, height), Image.BICUBIC)
    subject = subject.filter(ImageFilter.GaussianBlur(1.6)).point(lambda v: 0 if v < 70 else min(255, int((v - 70) * 1.6)))
    out = image.copy()
    out.putalpha(subject)
    return out, True



# --------------------------------------------------------------------------- look
def _on(accent) -> tuple[int, int, int]:
    """Readable text colour on an accent fill."""
    r, g, b = accent
    return (18, 22, 40) if (0.299 * r + 0.587 * g + 0.114 * b) > 165 else (255, 255, 255)


def _lerp(a, b, p: float):
    return tuple(round(a[i] + (b[i] - a[i]) * p) for i in range(len(a)))


def _background(theme_name: str, width: int = WIDTH, height: int = HEIGHT):
    """Gradient with blurred colour light, a faint dot grid, a vignette and film grain (deterministic)."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    theme = _PALETTE[theme_name]
    top, bottom = theme["bg_top"], theme["bg_bot"]
    column = np.linspace(0.0, 1.0, height)[:, None, None]
    sx, sy = width / WIDTH, height / HEIGHT
    k = 1.0 if (width, height) == (WIDTH, HEIGHT) else 0.8
    base = (np.array(top, dtype=np.float32) * (1 - column) + np.array(bottom, dtype=np.float32) * column)
    img = Image.fromarray(np.repeat(base, width, axis=1).astype(np.uint8), "RGB").convert("RGBA")
    for (cx, cy), radius, colour, strength in theme["blobs"]:
        cx, cy, radius = cx * sx, cy * sy, radius * k
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=colour + (int(255 * strength),))
        img = Image.alpha_composite(img, layer.filter(ImageFilter.GaussianBlur(radius * 0.55)))
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    ys, xs = np.mgrid[0:height, 0:width]
    step = max(36, round(54 * min(sx, sy) * (1.0 if width >= height else 1.2)))
    dots = ((xs % step) < 3) & ((ys % step) < 3)
    r, g, b, a = theme["dot_grid"]
    arr[dots] = arr[dots] * (1 - a / 255) + np.array((r, g, b), dtype=np.float32) * (a / 255)
    radial = (((xs - width / 2) / (width / 2)) ** 2 + ((ys - height / 2) / (height / 2)) ** 2) / 2
    arr *= (1 - theme["vignette"] * np.clip(radial, 0, 1))[:, :, None]
    rng = np.random.default_rng(7)
    arr += rng.normal(0, theme["grain"], (height, width, 1)).astype(np.float32)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def _rounded(size, radius, fill, scale=2):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size[0] * scale, size[1] * scale), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, size[0] * scale - 1, size[1] * scale - 1), radius * scale, fill=fill)
    return img


def _glass_card(w: int, h: int, radius: int, theme, accent, layout: str = "grid"):
    """Gradient glass card with a luminous edge, a soft shadow and an accent bar.

    The bar runs down the start (right) edge of a step row and across the top of every other card.
    """
    from PIL import Image, ImageDraw, ImageFilter

    pad, s = 80, 2
    canvas = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + 20, pad + w, pad + h + 20), radius, fill=theme["shadow"])
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(28)))
    ramp = Image.linear_gradient("L").resize((w * s, h * s))  # 0 at the top, 255 at the bottom
    body = Image.composite(Image.new("RGBA", (w * s, h * s), theme["card_bot"]),
                           Image.new("RGBA", (w * s, h * s), theme["card_top"]), ramp)
    d = ImageDraw.Draw(body)
    d.rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius * s, outline=theme["card_border"], width=3)
    # a thin light line along the upper edge reads as glass catching the light
    d.line((radius * s, 4, w * s - radius * s, 4), fill=(255, 255, 255, 46), width=2)
    if layout == "steps":
        d.rounded_rectangle((w * s - 18 * s, h * s * 0.2, w * s - 8 * s, h * s * 0.8), 5 * s, fill=accent + (255,))
    else:
        tab_w = min(160, w // 3) * s
        d.rounded_rectangle(((w * s - tab_w) / 2, 0, (w * s + tab_w) / 2, 8 * s), 4 * s, fill=accent + (255,))
    mask = _rounded((w, h), radius, (255, 255, 255, 255)).getchannel("A")
    clear = Image.new("RGBA", body.size, (0, 0, 0, 0))
    clear.paste(body, (0, 0), mask)
    canvas.alpha_composite(clear.resize((w, h), Image.LANCZOS), (pad, pad))
    return canvas, pad


def _plate(diameter: int, accent, theme, picture):
    """Tinted disc that carries the author's picture."""
    from PIL import Image, ImageDraw, ImageFilter

    s = 3
    pad = 30
    size = (diameter + pad * 2) * s
    c, r = size // 2, diameter * s // 2
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((c - r, c - r + 8 * s, c + r, c + r + 8 * s), fill=(0, 0, 0, 46))
    img = shadow.filter(ImageFilter.GaussianBlur(6 * s))
    disc = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    dd.ellipse((c - r, c - r, c + r, c + r), fill=accent + (theme["plate_alpha"],))
    dd.ellipse((c - r, c - r, c + r, c + r), outline=accent + (170,), width=3 * s)
    img = Image.alpha_composite(img, disc).resize((size // s, size // s), Image.LANCZOS)
    fit = diameter * 0.7
    k = min(fit / picture.width, fit / picture.height)
    art = picture.resize((max(2, round(picture.width * k)), max(2, round(picture.height * k))), Image.LANCZOS)
    img.alpha_composite(art, ((img.width - art.width) // 2, (img.height - art.height) // 2))
    return img


def _icon(path: Path):
    """The author's picture, cut out, with a faint contact shadow (no outline: it sits on a plate)."""
    from PIL import Image, ImageChops, ImageFilter

    with Image.open(path) as opened:
        opened.load()
        source = opened.copy()
    cut, keyed = _cutout(source)
    if keyed:
        bbox = cut.getchannel("A").point(lambda v: 255 if v > 10 else 0).getbbox()
        if not bbox:
            raise BoardError("board_picture_empty")
        cut = cut.crop(bbox)
    else:
        side = min(cut.size)
        cut = cut.crop(((cut.width - side) // 2, (cut.height - side) // 2, (cut.width + side) // 2, (cut.height + side) // 2))
        mask = _rounded(cut.size, side // 6, (255, 255, 255, 255), scale=1).getchannel("A")
        cut.putalpha(mask)
    pad = 24
    canvas = Image.new("RGBA", (cut.width + pad * 2, cut.height + pad * 2), (0, 0, 0, 0))
    shade = Image.new("L", canvas.size, 0)
    shade.paste(cut.getchannel("A"), (pad, pad))
    shade = ImageChops.offset(shade, 0, 8).filter(ImageFilter.GaussianBlur(9)).point(lambda v: int(v * 0.32))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow.putalpha(shade)
    canvas = Image.alpha_composite(canvas, shadow)
    canvas.alpha_composite(cut, (pad, pad))
    return canvas


def _text_layer(lines: Sequence[str], size: int, colour, width: int, align: str = "centre"):
    from PIL import Image, ImageDraw

    s = 2
    line_h = int(size * 1.22)
    img = Image.new("RGBA", (width * s, line_h * len(lines) * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = _font(size * s)
    for i, line in enumerate(lines):
        y = (i + 0.5) * line_h * s
        if align == "right":
            d.text((width * s, y), line, font=font, fill=colour, anchor="rm", direction="rtl", language="ar")
        else:
            d.text((width * s / 2, y), line, font=font, fill=colour, anchor="mm", direction="rtl", language="ar")
    return img.resize((width, line_h * len(lines)), Image.LANCZOS)


def _header(title: str, theme, accent, profile):
    """Right-aligned title with an accent bar (editorial, reads naturally in Arabic)."""
    from PIL import Image, ImageDraw

    page, right = profile["width"], profile["right_edge"]
    headline_size = max(profile["title_size"], 82) if profile["width"] == WIDTH else profile["title_size"]
    size, lines = _fit_label(title, profile["title_w"], headline_size, 44, 1)
    width = _text_size(lines[0], _font(size))[0]
    layer_w = profile["title_w"] + 150
    layer = _text_layer(lines, size, theme["ink"], layer_w, align="right")
    out = Image.new("RGBA", (page, max(240, profile["bar_y"] + 40)), (0, 0, 0, 0))
    out.alpha_composite(layer, (right - layer_w, int(profile["title_y"] - layer.height / 2)))
    bar = Image.new("RGBA", (page * 2, 40), (0, 0, 0, 0))
    ImageDraw.Draw(bar).rounded_rectangle((right * 2 - 150, 0, right * 2, 20), 10, fill=accent + (255,))
    out.alpha_composite(bar.resize((page, 20), Image.LANCZOS), (0, profile["bar_y"]))
    return out


RIGHT_EDGE = 1800
LEFT_EDGE = 120


def _dot(canvas, centre, radius: int, fill_amount: float, theme, accent):
    from PIL import Image, ImageDraw

    s = 4
    size = (radius * 2 + 8) * s
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = size // 2, radius * s
    d.ellipse((c - r, c - r, c + r, c + r), fill=theme["dot_off"])
    if fill_amount > 0:
        rr = r * (0.35 + 0.65 * min(1.0, fill_amount))
        d.ellipse((c - rr, c - rr, c + rr, c + rr), fill=accent + (int(255 * min(1.0, fill_amount)),))
    _paste(canvas, img.resize((size // s, size // s), Image.LANCZOS), centre)


# --------------------------------------------------------------------------- layout
def _geometry(layout: str, n: int, has_title: bool, profile) -> list[dict[str, Any]]:
    top = profile["top"] if has_title else profile["top_plain"]
    bottom = profile["bottom"]
    span = bottom - top
    compact = span < 600
    cells: list[dict[str, Any]] = []
    if layout == "row":
        gap = 46
        card_w = int(min(400, (profile["right_edge"] - profile["left_edge"] - gap * (n - 1)) / n))
        card_h = int(min(span - 10, 600 if not compact else 440))
        group = card_w * n + gap * (n - 1)
        right = (profile["width"] + group) / 2
        cy = top + span / 2
        for k in range(n):
            cx = right - card_w / 2 - k * (card_w + gap)
            cells.append({"cx": cx, "cy": cy, "w": card_w, "h": card_h, "gap": gap, "plate": int(min(card_w * 0.7, card_h * 0.52, 280))})
    elif layout == "compare":
        # Two parallel states without the causal arrow used by row.
        gap = 70
        usable = profile["right_edge"] - profile["left_edge"]
        card_w = int(min(725, (usable - gap) / 2))
        card_h = int(min(span - 10, 600 if not compact else 440))
        cy = top + span / 2
        centre_offset = (card_w + gap) / 2
        for k in range(2):
            cells.append({"cx": profile["width"] / 2 + (centre_offset if k == 0 else -centre_offset),
                          "cy": cy, "w": card_w, "h": card_h,
                          "plate": int(min(card_w * 0.35, card_h * 0.43, 210))})
    elif layout == "steps":
        row_h = min(190, (span - 4) / n)
        for k in range(n):
            cells.append({"cx": profile["width"] / 2, "cy": top + 2 + row_h * (k + 0.5), "w": profile["steps_w"], "h": int(row_h - (12 if compact else 24)),
                          "plate": int(min(row_h - 36, 140))})
    elif layout == "grid":
        cols = 2 if n <= 4 else 3 if n <= 6 else 4
        rows = math.ceil(n / cols)
        gap = 34
        cell_w = (profile["right_edge"] - profile["left_edge"] - gap * (cols - 1)) / cols
        cell_h = (span - gap * (rows - 1)) / rows
        for k in range(n):
            r, c = divmod(k, cols)
            in_row = cols if r < rows - 1 else n - cols * (rows - 1)
            right = (profile["width"] + in_row * cell_w + (in_row - 1) * gap) / 2
            cells.append({"cx": right - cell_w / 2 - c * (cell_w + gap), "cy": top + cell_h / 2 + r * (cell_h + gap),
                          "w": int(cell_w), "h": int(cell_h), "plate": int(min(cell_h * (0.46 if compact else 0.42), cell_w * 0.5, 190))})
    else:  # hub
        cx0, cy0 = 960, 560
        rx, ry = 660, 318
        for k in range(n):
            angle = math.radians(-90 + 360 * k / n)
            cells.append({"cx": cx0 + rx * math.cos(angle), "cy": cy0 + ry * math.sin(angle), "w": 360, "h": 250,
                          "plate": 130, "hub": (cx0, cy0)})
    return cells


class _Item:
    def __init__(self, index: int, spec: Mapping[str, Any], cell: dict[str, Any], layout: str, theme_name: str, root: Path):
        theme = _PALETTE[theme_name]
        self.index, self.cell, self.layout, self.theme = index, cell, layout, theme
        self.accent = theme["accents"][index % len(theme["accents"])]
        label = str(spec["label"]).strip()
        w, h, plate = cell["w"], cell["h"], cell["plate"]
        picture = _icon(root / spec["asset"]) if spec.get("asset") else None
        # A picture sits on a tinted disc; without one the number itself is the visual.
        self.plate = _plate(plate, self.accent, theme, picture) if picture is not None else None
        self.card, self.card_pad = _glass_card(w, h, 44 if layout != "steps" else 40, theme, self.accent, layout)
        self._compose_card(label)
        self.glow = None

    def _compose_card(self, label: str) -> None:
        w, h, plate = self.cell["w"], self.cell["h"], self.cell["plate"]
        pad = self.card_pad
        if self.layout == "steps":
            node = 88
            node_right = 64  # distance from the card's right edge to the number badge's right edge
            label_right = node_right + node + 34
            left_room = (64 + plate + 30) if self.plate is not None else 60
            label_w = int(min(1050, w - label_right - left_room))
            size, lines = _fit_label(label, label_w, 84, 46)
            text = _text_layer(lines, size, self.theme["ink"], label_w + 20, align="right")
            self.card.alpha_composite(text, (pad + w - label_right - (label_w + 20), int(pad + h / 2 - text.height / 2)))
            badge = _badge(self.index + 1, node, self.accent)
            self.card.alpha_composite(badge, (int(pad + w - node_right - node / 2 - badge.width / 2), int(pad + h / 2 - badge.height / 2)))
            self.plate_at = (pad + 64 + plate / 2, pad + h / 2)  # picture on the left of the row
        else:
            font_size = 76 if w >= 560 else 62 if w >= 400 else 52
            if self.layout == "hub":
                font_size = 46
            size, lines = _fit_label(label, w - 56, font_size, 32)
            text = _text_layer(lines, size, self.theme["ink"], w - 28)
            bottom_gap = 28 if self.layout != "hub" else 20
            self.card.alpha_composite(text, (pad + 14, int(pad + h - bottom_gap - text.height)))
            top_room = h - bottom_gap - text.height - 36
            self.plate_at = (pad + w / 2, pad + 36 + top_room / 2)
            if self.plate is None:
                numeral = _text_layer([f"{self.index + 1:02d}"], int(max(40, min(150, top_room * 0.62))), self.accent + (255,), w)
                self.card.alpha_composite(numeral, (pad, int(pad + 36 + top_room / 2 - numeral.height / 2 - 6)))
                rule = int(pad + 36 + top_room / 2 + numeral.height / 2 - 2)
                bar = _rounded((64, 6), 3, self.accent + (210,), scale=1)
                if rule + 6 < pad + h - bottom_gap - text.height - 8:
                    self.card.alpha_composite(bar, (int(pad + w / 2 - 32), rule))

    def origin(self):
        return self.cell["cx"] - self.cell["w"] / 2 - self.card_pad, self.cell["cy"] - self.cell["h"] / 2 - self.card_pad


def _badge(number: int, diameter: int, accent):
    from PIL import Image, ImageDraw

    s = 3
    pad = 20
    size = (diameter + pad * 2) * s
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = size // 2, diameter * s // 2
    d.ellipse((c - r, c - r, c + r, c + r), fill=accent + (255,))
    d.ellipse((c - r + 5 * s, c - r + 5 * s, c + r - 5 * s, c + r - 5 * s), outline=(255, 255, 255, 70), width=2 * s)
    d.text((c, c - s * 2), str(number), font=_font(int(diameter * 0.55) * s), fill=_on(accent), anchor="mm")
    return img.resize((size // s, size // s), Image.LANCZOS)


def _paste(canvas, layer, centre, scale: float = 1.0, alpha: float = 1.0, offset=(0, 0)):
    from PIL import Image

    if alpha <= 0.003 or scale <= 0.01:
        return
    if abs(scale - 1.0) > 0.002:
        layer = layer.resize((max(2, round(layer.width * scale)), max(2, round(layer.height * scale))), Image.BICUBIC)
    if alpha < 0.997:
        layer = layer.copy()
        layer.putalpha(layer.getchannel("A").point(lambda v: int(v * alpha)))
    x = round(centre[0] + offset[0] - layer.width / 2)
    y = round(centre[1] + offset[1] - layer.height / 2)
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(layer.width, canvas.width - x), min(layer.height, canvas.height - y)
    if right > left and bottom > top:
        canvas.alpha_composite(layer.crop((left, top, right, bottom)), (max(0, x), max(0, y)))


def _chip(canvas, centre, accent, progress: float):
    """Round arrow chip that sits in the gap between two row cards (flow direction: right to left)."""
    from PIL import Image, ImageDraw

    if progress <= 0.01:
        return
    s = 4
    d_px = 62
    size = (d_px + 40) * s
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, r = size // 2, d_px * s // 2
    d.ellipse((c - r, c - r + 6 * s, c + r, c + r + 6 * s), fill=(0, 0, 0, 60))
    d.ellipse((c - r, c - r, c + r, c + r), fill=accent + (255,))
    ink = _on(accent) + (255,)
    d.line([(c + 8 * s, c - 14 * s), (c - 8 * s, c), (c + 8 * s, c + 14 * s)], fill=ink, width=6 * s, joint="curve")
    d.ellipse((c + 8 * s - 3 * s, c - 14 * s - 3 * s, c + 8 * s + 3 * s, c - 14 * s + 3 * s), fill=ink)
    d.ellipse((c + 8 * s - 3 * s, c + 14 * s - 3 * s, c + 8 * s + 3 * s, c + 14 * s + 3 * s), fill=ink)
    img = img.resize((size // s, size // s), Image.LANCZOS)
    _paste(canvas, img, centre, scale=0.4 + 0.6 * _out_back(progress), alpha=min(1.0, progress * 2.2))


def _line(canvas, p1, p2, colour, progress: float, width: int = 5):
    """Thin gradient-free line drawn from p1 toward p2 with round caps and a dot at the head."""
    from PIL import Image, ImageDraw

    progress = min(1.0, max(0.0, progress))
    if progress <= 0.001:
        return
    s = 3
    pad = 24
    x0, y0 = min(p1[0], p2[0]) - pad, min(p1[1], p2[1]) - pad
    w, h = int(abs(p2[0] - p1[0]) + pad * 2), int(abs(p2[1] - p1[1]) + pad * 2)
    layer = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    a = ((p1[0] - x0) * s, (p1[1] - y0) * s)
    b = ((p1[0] + (p2[0] - p1[0]) * progress - x0) * s, (p1[1] + (p2[1] - p1[1]) * progress - y0) * s)
    d.line([a, b], fill=colour + (170,), width=width * s)
    r = 9 * s
    d.ellipse((b[0] - r, b[1] - r, b[0] + r, b[1] + r), fill=colour + (255,))
    _paste(canvas, layer.resize((w, h), Image.LANCZOS), (x0 + w / 2, y0 + h / 2))


def _glow(item: _Item, alpha: float, canvas):
    from PIL import Image, ImageDraw, ImageFilter

    if alpha <= 0.01:
        return
    if item.glow is None:
        w, h = item.cell["w"] + 220, item.cell["h"] + 220
        img = Image.new("RGBA", (w + 200, h + 200), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle((100 + 70, 100 + 70, w + 130, h + 130), 90, fill=item.accent + (255,))
        img = img.filter(ImageFilter.GaussianBlur(52))
        k = float(item.theme.get("glow", 0.5))
        img.putalpha(img.getchannel("A").point(lambda v: int(v * k)))
        item.glow = img
    _paste(canvas, item.glow, (item.cell["cx"], item.cell["cy"]), alpha=alpha)


def _hub_core(title: str, theme, accent):
    from PIL import Image, ImageDraw, ImageFilter

    s = 2
    size = 420
    img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    c = size * s / 2
    glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((c - 150 * s, c - 150 * s, c + 150 * s, c + 150 * s), fill=accent + (150,))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(40 * s)))
    d = ImageDraw.Draw(img)
    r = 138 * s
    d.ellipse((c - r, c - r, c + r, c + r), fill=accent + (255,))
    d.ellipse((c - r + 12 * s, c - r + 12 * s, c + r - 12 * s, c + r - 12 * s), outline=(255, 255, 255, 90), width=3 * s)
    d.ellipse((c - r - 26 * s, c - r - 26 * s, c + r + 26 * s, c + r + 26 * s), outline=accent + (90,), width=2 * s)
    label_size, lines = _fit_label(title or "", 210, 46, 28, 2) if title else (40, [""])
    layer = _text_layer(lines, label_size * s, _on(accent), 240 * s)
    img.alpha_composite(layer, (int(c - layer.width / 2), int(c - layer.height / 2)))
    return img.resize((size, size), Image.LANCZOS)


def _draw_item(canvas, item: _Item, items: Sequence[_Item], u: float, final: bool) -> None:
    """Draw item at u seconds after its reveal. final=True draws the settled form (no glow)."""
    cell = item.cell
    ox, oy = item.cell["cx"], item.cell["cy"]
    if final:
        card_p = plate_p = chip_p = 1.0
    else:
        card_p = _out_cubic(u / 0.5)
        plate_p = (u - 0.1) / 0.5
        chip_p = (u + 0.12) / 0.4
        _glow(item, max(0.0, 1 - u / SETTLE_SECONDS) * min(1.0, u / 0.25), canvas)
    if item.layout == "hub":
        a = cell["hub"]
        ang = math.atan2(oy - a[1], ox - a[0])
        reach = 150
        end_r = min(cell["w"] / (2 * max(0.2, abs(math.cos(ang)))), cell["h"] / (2 * max(0.2, abs(math.sin(ang))))) * 0.98
        _line(canvas, (a[0] + reach * math.cos(ang), a[1] + reach * math.sin(ang)),
              (ox - end_r * math.cos(ang), oy - end_r * math.sin(ang)), item.accent, 1.0 if final else _out_cubic((u + 0.1) / 0.5))
    slide = (1 - card_p) * 40
    _paste(canvas, item.card, (ox, oy), scale=0.965 + 0.035 * card_p, alpha=min(1.0, card_p * 1.6), offset=(0, slide))
    if item.plate is not None:
        px, py = item.plate_at
        left, top = ox - cell["w"] / 2 - item.card_pad, oy - cell["h"] / 2 - item.card_pad
        p = 1.0 if final else _out_back(plate_p)
        _paste(canvas, item.plate, (left + px, top + py), scale=(1.0 if final else 0.5 + 0.5 * p),
               alpha=1.0 if final else min(1.0, max(0.0, plate_p * 2.5)), offset=(0, 0 if final else slide))
    if item.layout == "row" and item.index > 0:
        prev = items[item.index - 1].cell
        arrow_at = ((prev["cx"] - prev["w"] / 2 + ox + cell["w"] / 2) / 2, oy)
        _chip(canvas, arrow_at, item.accent, 1.0 if final else chip_p)


# --------------------------------------------------------------------------- timing
def reveal_times(scene_text: str, items: Sequence[Mapping[str, Any]], seconds: float, *, compress: float = 1.0) -> list[float]:
    """Seconds (from clip start) at which each item appears, in item order.

    The fraction is the item's sentence position inside the scene's own words, the same estimate
    the renderer uses for scene anchors. `compress` < 1 pulls every reveal earlier, used when
    only an estimated duration is known so that a shorter real slot still shows everything.
    """
    text = " ".join(str(scene_text).split())
    total_words = max(1, len(text.split()))
    raw: list[float] = []
    for item in items:
        needle = " ".join(str(item.get("at_text") or "").split())
        index = text.find(needle) if needle else -1
        fraction = (len(text[:index].split()) / total_words) if index >= 0 else (len(raw) / max(1, len(items)))
        raw.append(fraction * seconds * compress - LEAD_SECONDS)
    n = len(raw)
    last_allowed = max(REVEAL_FIRST, seconds - REVEAL_TAIL)
    spacing = REVEAL_SPACING
    if REVEAL_FIRST + spacing * (n - 1) > last_allowed:
        spacing = max(0.3, (last_allowed - REVEAL_FIRST) / max(1, n - 1))
    times: list[float] = []
    for k, value in enumerate(raw):
        floor = REVEAL_FIRST if k == 0 else times[-1] + spacing
        times.append(max(value, floor))
    # pull the tail back inside the clip while keeping the spacing
    overflow = times[-1] - last_allowed
    if overflow > 0:
        for k in range(n - 1, -1, -1):
            ceiling = last_allowed - spacing * (n - 1 - k)
            times[k] = min(times[k], ceiling)
        for k in range(1, n):
            times[k] = max(times[k], times[k - 1] + min(spacing, 0.3))
    return [round(t, 3) for t in times]



# --------------------------------------------------------------------------- render
def render_board_clip(board: Mapping[str, Any], root: Path, destination: Path, *, seconds: float,
                      times: Sequence[float], fmt: str = "film") -> Path:
    """Render the board to destination (1920x1080, or 1080x1920 for a Short; 30 fps, h264). Raises BoardError on any problem."""
    layout, theme_name = str(board["layout"]), str(board.get("theme") or "slate")
    specs = list(board["items"])
    allowed = PODCAST_LAYOUTS if fmt == "podcast" else SHORT_LAYOUTS if fmt == "short" else LAYOUTS
    profile = PROFILES[fmt if fmt in {"podcast", "short"} else "film"]
    width, height = profile["width"], profile["height"]
    if layout not in allowed or theme_name not in THEMES or len(times) != len(specs):
        raise BoardError("board_spec_invalid")
    title = str(board.get("title") or "").strip()
    theme = _PALETTE[theme_name]
    geometry = _geometry(layout, len(specs), bool(title) or layout == "hub", profile)
    items = [_Item(i, spec, geometry[i], layout, theme_name, Path(root)) for i, spec in enumerate(specs)]
    background = _background(theme_name, width, height)
    frames = max(2, round(float(seconds) * FPS))
    header = _header(title, theme, theme["accents"][0], profile) if title and layout != "hub" else None
    hub = _hub_core(title, theme, theme["accents"][0]) if layout == "hub" else None
    dot_y = profile["title_y"] if header is not None else profile["dots_y"]
    dots = [(profile["left_edge"] + 12 + k * 30, dot_y) for k in range(len(items))]

    tmp = Path(destination).with_suffix(".board.mp4")
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-", "-an", "-c:v", "libx264", "-preset", "ultrafast",
         "-tune", "animation", "-crf", "17", "-pix_fmt", "yuv420p", "-vf", "setsar=1", str(tmp)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        base = background.copy()
        for k, centre in enumerate(dots):
            _dot(base, centre, 7, 0.0, theme, items[k].accent)
        header_done = hub_done = False
        settled = [False] * len(items)
        last_key = None
        last_bytes = b""
        hub_centre = geometry[0]["hub"] if hub is not None else None
        for k in range(frames):
            t = k / FPS
            if header is not None and not header_done and t - 0.12 >= 0.6:
                _paste(base, header, (width / 2, header.height / 2)); header_done = True
            if hub is not None and not hub_done and t - 0.1 >= 0.7:
                _paste(base, hub, hub_centre); hub_done = True
            for i, item in enumerate(items):
                if not settled[i] and t >= times[i] + SETTLE_SECONDS:
                    _draw_item(base, item, items, SETTLE_SECONDS, True)
                    _dot(base, dots[i], 7, 1.0, theme, item.accent)
                    settled[i] = True
            active = [i for i in range(len(items)) if not settled[i] and t >= times[i] - 0.12]
            intro = (header is not None and not header_done) or (hub is not None and not hub_done)
            key = None if (active or intro) else (tuple(settled), header_done, hub_done)
            if key is not None and key == last_key:
                proc.stdin.write(last_bytes)
                continue
            frame = base.copy()
            if header is not None and not header_done:
                p = _out_cubic((t - 0.12) / 0.6)
                _paste(frame, header, (width / 2, header.height / 2), alpha=p, offset=(26 * (1 - p), 0))
            if hub is not None and not hub_done:
                p = _out_back((t - 0.1) / 0.7)
                _paste(frame, hub, hub_centre, scale=0.7 + 0.3 * p, alpha=min(1.0, max(0.0, (t - 0.1) / 0.3)))
            for i in active:
                u = max(0.0, t - times[i])
                _draw_item(frame, items[i], items, u, False)
                _dot(frame, dots[i], 7, min(1.0, u / 0.3), theme, items[i].accent)
            last_bytes = frame.convert("RGB").tobytes()
            last_key = key
            proc.stdin.write(last_bytes)
        proc.stdin.close()
        error = proc.stderr.read().decode("utf-8", "replace")
        proc.stderr.close()
        if proc.wait(timeout=900) != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
            raise BoardError("board_encode_failed:" + error[-200:])
        tmp.replace(destination)
        return Path(destination)
    except BaseException:
        try:
            proc.kill()
            proc.wait(timeout=30)
            proc.stderr.close()
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        tmp.unlink(missing_ok=True)
        raise



# --------------------------------------------------------------------------- pipeline hooks
REPORT_NAME = "board-report.json"


def rerender_scheduled(output_dir: Path, schedule: Sequence[Mapping[str, Any]], rows: Sequence[Mapping[str, Any]],
                       fmt: str) -> list[dict[str, Any]]:
    """Re-render every board clip to its exact measured slot, in place. Never raises.

    The acquisition-time clip was built from an estimate (reveals pulled early so a shorter slot
    still shows everything). Once the voice timeline fixes the slot, each item is placed on the
    sentence that introduces it. A failure keeps the estimate clip, which is already a full board.
    """
    import json

    by_file = {str(r.get("local_file")): r for r in rows if isinstance(r, Mapping)}
    report: list[dict[str, Any]] = []
    for slot in schedule:
        path = Path(slot["path"])
        row = by_file.get(path.name)
        info = row.get("owner_board") if row else None
        if not isinstance(info, Mapping):
            continue
        seconds = float(slot["end"]) - float(slot["start"])
        entry: dict[str, Any] = {"file": path.name, "beat_id": row.get("beat_id"), "start": round(float(slot["start"]), 3),
                                 "end": round(float(slot["end"]), 3)}
        try:
            spec = info["spec"]
            times = reveal_times(str(info["text"]), spec["items"], seconds)
            exact = path.with_name(path.stem + ".exact.mp4")
            render_board_clip(spec, Path(str(row["owner_board_root"])), exact, seconds=seconds + 0.2, times=times, fmt=fmt)
            exact.replace(path)
            entry.update(status="exact", reveal_seconds=times)
        except Exception as exc:  # the estimate clip stays: a complete board, never a gap
            entry.update(status="kept_estimate", reason=f"{type(exc).__name__}:{str(exc)[:160]}")
        report.append(entry)
    if report:
        try:
            (Path(output_dir) / REPORT_NAME).write_text(json.dumps({"slots": report}, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return report


def blocked_windows(output_dir: Path, margin: float = 0.4) -> list[tuple[float, float]]:
    """Time windows in which a board is on screen (Film key text must stay out of them)."""
    import json

    try:
        data = json.loads((Path(output_dir) / REPORT_NAME).read_text(encoding="utf-8"))
        return [(float(x["start"]) - margin, float(x["end"]) + margin) for x in data.get("slots", [])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def drop_events_over_boards(events: Sequence[Mapping[str, Any]], windows: Sequence[tuple[float, float]]) -> list[Mapping[str, Any]]:
    return [e for e in events if not any(float(e["start"]) < end and float(e["end"]) > begin for begin, end in windows)]
