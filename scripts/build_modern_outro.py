"""Build the Short / على مهل outro: 67 frames @ 30 fps, 1080x1920, with its own sound.

Replaces the old outros (a red generic subscribe button, an arrow cursor and a silent audio
track). The look is the channel's own (glass card, brand accent, touch dot, bell) and the
sound is synthesised here (tap + confirmation chime + bell shimmer), so nothing is licensed
or downloaded. Run:  python scripts/build_modern_outro.py [--out DIR] [--preview DIR]

The logo is taken from the existing outro file (frame 0), so the brand mark is unchanged.
"""
from __future__ import annotations

import argparse
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ASSET_DIR = Path(__file__).resolve().parents[1] / "clean_v2" / "assets" / "identity"
W, H, FPS, FRAMES = 1080, 1920, 30, 67
SR = 44100
DURATION = FRAMES / FPS

# Frame timelines. The Short outro only plays its first 1.25 s (identity profile final_silence_seconds),
# so its whole story (tap -> red -> bell) happens early; على مهل plays all 67 frames and breathes more.
TIMELINES = {
    "short_outro.mp4": {"fade": 6, "appear": (1, 9), "dot": (2, 17), "tap": 19, "bell": 23, "text": (26, 34)},
    "ala_mahl_outro.mp4": {"fade": 12, "appear": (4, 18), "dot": (8, 31), "tap": 33, "bell": 40, "text": (44, 54)},
}
TL: dict = dict(TIMELINES["ala_mahl_outro.mp4"])
TAP_FRAME = TL["tap"]
CONFIRM_FRAME = TL["tap"] + 2
BELL_FRAME = TL["bell"]


def _use_timeline(name: str) -> None:
    global TAP_FRAME, CONFIRM_FRAME, BELL_FRAME
    TL.clear()
    TL.update(TIMELINES[name])
    TAP_FRAME, CONFIRM_FRAME, BELL_FRAME = TL["tap"], TL["tap"] + 2, TL["bell"]

BRANDS = {
    "short_outro.mp4": {
        "bg": (6, 9, 17), "glow": (201, 162, 75), "accent": (201, 162, 75), "accent_hi": (236, 205, 128),
        "glass": (14, 22, 40), "ink": (11, 18, 32), "cream": (244, 238, 226),
    },
    "ala_mahl_outro.mp4": {
        "bg": (16, 11, 8), "glow": (214, 178, 128), "accent": (216, 184, 138), "accent_hi": (243, 222, 190),
        "glass": (36, 25, 18), "ink": (38, 26, 18), "cream": (250, 243, 232),
    },
}

RED_HI, RED_LO = (255, 62, 56), (222, 22, 22)  # the colour only appears after the tap

ARABIC_FONT = "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont:
    # Prefer Cairo Bold (the channel font) when it is installed; otherwise fall back to Noto.
    import os, subprocess
    cairo = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "fonts" / "isco-cairo" / "Cairo.ttf"
    if not cairo.is_file():
        out = subprocess.run(["fc-match", "-f", "%{family}|%{file}", "Cairo:weight=bold"], capture_output=True, text=True).stdout
        fam, _, f = out.partition("|")
        cairo = Path(f) if "cairo" in fam.lower() and Path(f).is_file() else cairo
    path = next((p for p in (str(cairo), ARABIC_FONT) if Path(p).is_file()), None)
    if path is None:
        raise SystemExit("no Arabic font available for the outro")
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def ease_in_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return t * t * (3 - 2 * t)


def seg(frame: float, a: float, b: float) -> float:
    return min(1.0, max(0.0, (frame - a) / max(1e-6, b - a)))


# ----------------------------------------------------------------------------- drawing helpers
SS = 2  # supersampling for the dynamic layers


def _text(draw: ImageDraw.ImageDraw, xy, value: str, size: int, fill, *, bold: int = 2, anchor="mm", ltr: bool = False) -> None:
    kwargs = {"direction": "ltr"} if ltr else {"direction": "rtl", "language": "ar"}
    draw.text(xy, value, font=_font(size), anchor=anchor, fill=fill, stroke_width=bold, stroke_fill=fill, **kwargs)


def _bell(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill, angle: float = 0.0) -> None:
    """Vector bell, rotated about its top by `angle` degrees. `s` is the half height."""
    pts = []
    for i in range(0, 181, 6):  # dome
        a = math.radians(180 - i)
        pts.append((math.cos(a) * 0.62, -0.18 - math.sin(a) * 0.62))
    pts += [(0.62, 0.10), (0.86, 0.46), (-0.86, 0.46), (-0.62, 0.10)]
    top = (0.0, -0.95)
    rot = math.radians(angle)

    def tf(p):
        x, y = p[0], p[1] + 0.95
        rx = x * math.cos(rot) - y * math.sin(rot)
        ry = x * math.sin(rot) + y * math.cos(rot)
        return (cx + rx * s, cy + (ry - 0.95) * s)

    draw.polygon([tf(p) for p in pts], fill=fill)
    draw.ellipse([tf((-0.14, -0.99))[0], tf((-0.14, -0.99))[1], tf((0.14, -0.71))[0], tf((0.14, -0.71))[1]], fill=fill)
    c = tf((0.0, 0.62))
    draw.ellipse([c[0] - 0.19 * s, c[1] - 0.19 * s, c[0] + 0.19 * s, c[1] + 0.19 * s], fill=fill)
    _ = top


def _check(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float, fill, progress: float = 1.0) -> None:
    p1, p2, p3 = (cx - 0.55 * s, cy + 0.02 * s), (cx - 0.15 * s, cy + 0.42 * s), (cx + 0.62 * s, cy - 0.42 * s)
    width = max(2, int(0.26 * s))
    if progress <= 0:
        return
    first = min(1.0, progress / 0.4)
    q = (p1[0] + (p2[0] - p1[0]) * first, p1[1] + (p2[1] - p1[1]) * first)
    draw.line([p1, q], fill=fill, width=width, joint="curve")
    if progress > 0.4:
        second = (progress - 0.4) / 0.6
        r = (p2[0] + (p3[0] - p2[0]) * second, p2[1] + (p3[1] - p2[1]) * second)
        draw.line([p2, r], fill=fill, width=width, joint="curve")
    for p in (p1, p2):
        draw.ellipse([p[0] - width / 2, p[1] - width / 2, p[0] + width / 2, p[1] + width / 2], fill=fill)


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def background(brand: dict, logo: Image.Image) -> Image.Image:
    """Dark gradient + soft brand glow behind the logo; the logo is blended with 'lighten'."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    bg = np.zeros((H, W, 3), np.float32)
    base = np.array(brand["bg"], np.float32)
    glow = np.array(brand["glow"], np.float32)
    r = np.sqrt(((xx - W / 2) / 520) ** 2 + ((yy - 455) / 520) ** 2)
    g = np.clip(1 - r, 0, 1) ** 2.2
    lower = np.clip(1 - np.sqrt(((xx - W / 2) / 700) ** 2 + ((yy - 1130) / 380) ** 2), 0, 1) ** 2
    bg[:] = base
    bg += glow * (0.20 * g + 0.045 * lower)[..., None]
    # faint vignette
    v = np.clip(np.sqrt(((xx - W / 2) / 760) ** 2 + ((yy - H / 2) / 1180) ** 2), 0, 1)
    bg *= (1 - 0.35 * v ** 2)[..., None]
    canvas = Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8), "RGB")
    # Keep only the round logo (soft radial edge): the crop's own background would otherwise leave a seam.
    ly, lx = np.mgrid[0:logo.height, 0:W].astype(np.float32)
    dist = np.sqrt((lx - W / 2) ** 2 + (ly - 355) ** 2)
    keep = np.clip((395 - dist) / 70, 0, 1)
    arr = np.asarray(logo, np.float32) * keep[..., None]
    layer = Image.new("RGB", (W, H), (0, 0, 0))
    layer.paste(Image.fromarray(arr.astype(np.uint8), "RGB"), (0, 100))
    from PIL import ImageChops
    return ImageChops.lighter(canvas, layer)


# Geometry of the subscribe card (box coordinates; the box sits at BOX_X, BOX_Y on the 1080x1920 frame).
BOX_W, BOX_H, BOX_X, BOX_Y = 900, 560, 90, 940
CARD = (24, 40, 876, 280)            # x0, y0, x1, y1
CARD_CY = (CARD[1] + CARD[3]) / 2
BADGE_X, BADGE_R = 120, 60           # the "+1" slot, on the left like the old outro
BELL_X, BELL_R = 780, 66             # the bell chip, on the right (start of an Arabic line)
PILL_X0, PILL_X1 = 206, 688
WORD_CENTER = (BOX_X + (PILL_X0 + PILL_X1) / 2, BOX_Y + CARD_CY)  # where the arrow presses: the word "اشترك"


def _pop(t: float) -> float:
    """Scale curve with a small overshoot (0 -> 1.12 -> 1)."""
    t = min(1.0, max(0.0, t))
    return 1 + 2.2 * (t - 1) ** 3 + 1.2 * (t - 1) ** 2


def module_layer(brand: dict, frame: int) -> Image.Image:
    """The subscribe card for one frame as RGBA (W x H, transparent elsewhere)."""
    cream, accent, accent_hi, glass, ink = (brand[k] for k in ("cream", "accent", "accent_hi", "glass", "ink"))
    S = SS
    img = Image.new("RGBA", (BOX_W * S, BOX_H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    appear = ease_out(seg(frame, *TL["appear"]))
    lift = (1 - appear) * 36
    subscribed = frame >= CONFIRM_FRAME

    # --- the card: floating glass
    cx0, cy0, cx1, cy1 = CARD[0], CARD[1] + lift, CARD[2], CARD[3] + lift
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([cx0 * S, (cy0 + 14) * S, cx1 * S, (cy1 + 14) * S], radius=120 * S, fill=(0, 0, 0, int(150 * appear)))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(26 * S)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([cx0 * S, cy0 * S, cx1 * S, cy1 * S], radius=120 * S, fill=glass + (int(226 * appear),))
    d.rounded_rectangle([cx0 * S, cy0 * S, cx1 * S, cy1 * S], radius=120 * S, outline=_lerp(accent, (255, 255, 255), 0.15) + (int(190 * appear),), width=3 * S)
    d.rounded_rectangle([(cx0 + 36) * S, (cy0 + 8) * S, (cx1 - 36) * S, (cy0 + 14) * S], radius=3 * S, fill=(255, 255, 255, int(46 * appear)))
    cy = CARD_CY + lift

    # --- "+1" slot on the left: a faint ring before the tap, a pop with the number after it
    bx0, by0 = BADGE_X, cy
    if not subscribed:
        d.ellipse([(bx0 - BADGE_R) * S, (by0 - BADGE_R) * S, (bx0 + BADGE_R) * S, (by0 + BADGE_R) * S],
                  outline=accent + (int(46 * appear),), width=2 * S)
    else:
        t = seg(frame, CONFIRM_FRAME + 1, CONFIRM_FRAME + 8)
        sc = max(0.05, _pop(t))
        r = BADGE_R * sc
        grad = Image.new("RGBA", (int(2 * r * S) + 2, int(2 * r * S) + 2), (0, 0, 0, 0))
        gd = ImageDraw.Draw(grad)
        for y in range(grad.height):
            gd.line([(0, y), (grad.width, y)], fill=_lerp(accent_hi, accent, y / grad.height) + (255,))
        mask = Image.new("L", grad.size, 0)
        ImageDraw.Draw(mask).ellipse([0, 0, grad.width - 1, grad.height - 1], fill=255)
        img.paste(grad, (int(bx0 * S - grad.width / 2), int(by0 * S - grad.height / 2)), mask)
        d = ImageDraw.Draw(img)
        a = int(255 * min(1.0, t * 2.2))
        rise = (1 - ease_out(seg(frame, CONFIRM_FRAME + 1, CONFIRM_FRAME + 12))) * 10
        _text(d, (bx0 * S, (by0 - 3 + rise) * S), "+1", int(66 * S * min(1.0, 0.6 + 0.4 * sc)), ink + (a,), bold=S, ltr=True)
        # a soft halo ring that expands once
        halo = seg(frame, CONFIRM_FRAME + 2, CONFIRM_FRAME + 14)
        if 0 < halo < 1:
            rr = BADGE_R + 6 + 30 * ease_out(halo)
            d.ellipse([(bx0 - rr) * S, (by0 - rr) * S, (bx0 + rr) * S, (by0 + rr) * S],
                      outline=accent_hi + (int(170 * (1 - halo)),), width=3 * S)

    # --- bell chip on the right: swings once the button is subscribed
    ring_t = seg(frame, BELL_FRAME, BELL_FRAME + 22)
    swing = math.sin(ring_t * math.pi * 5) * 24 * (1 - ring_t) if frame >= BELL_FRAME else 0.0
    chip_fill = (accent + (int(255 * appear),)) if frame >= BELL_FRAME else (255, 255, 255, int(22 * appear))
    d.ellipse([(BELL_X - BELL_R) * S, (cy - BELL_R) * S, (BELL_X + BELL_R) * S, (cy + BELL_R) * S], fill=chip_fill,
              outline=_lerp(accent, (255, 255, 255), 0.1) + (int(210 * appear),), width=3 * S)
    bell_col = (ink + (255,)) if frame >= BELL_FRAME else (accent_hi + (int(255 * appear),))
    _bell(d, BELL_X * S, (cy - 4) * S, 38 * S, bell_col, angle=swing)

    # --- main pill: brand accent before the tap, red after it
    px0, px1 = PILL_X0, PILL_X1
    py0, py1 = cy - 74, cy + 74
    pr = (py1 - py0) / 2
    press = 0.0
    if TAP_FRAME - 3 <= frame <= TAP_FRAME + 4:
        press = math.sin(seg(frame, TAP_FRAME - 3, TAP_FRAME + 4) * math.pi)
    shrink = 12 * press
    w_pill, h_pill = int(px1 - px0 - shrink), int(py1 - py0 - shrink)
    colours = (RED_HI, RED_LO) if subscribed else (accent_hi, accent)
    grad = Image.new("RGBA", (w_pill * S, h_pill * S))
    gd = ImageDraw.Draw(grad)
    for y in range(grad.height):
        gd.line([(0, y), (grad.width, y)], fill=_lerp(colours[0], colours[1], y / grad.height) + (255,))
    mask = Image.new("L", grad.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, grad.width - 1, grad.height - 1], radius=int(h_pill / 2 * S), fill=int(255 * appear))
    img.paste(grad, (int((px0 + shrink / 2) * S), int((py0 + shrink / 2) * S)), mask)
    d = ImageDraw.Draw(img)
    mid = (px0 + px1) / 2
    if not subscribed:
        _text(d, (mid * S, (cy - 2) * S), "اشترك", 92 * S, ink + (int(255 * appear),), bold=3 * S // 2)
    else:
        t = ease_out(seg(frame, CONFIRM_FRAME, CONFIRM_FRAME + 8))
        _check(d, (px0 + 62) * S, cy * S, 22 * S, (255, 255, 255, 255), progress=seg(frame, CONFIRM_FRAME, CONFIRM_FRAME + 9))
        _text(d, ((mid + 26) * S, (cy - 2) * S), "تم الاشتراك", 66 * S, (255, 255, 255, int(255 * t)), bold=S)

    # --- tap ripple under the arrow tip (the word "اشترك")
    if TAP_FRAME <= frame <= TAP_FRAME + 12:
        r_t = seg(frame, TAP_FRAME, TAP_FRAME + 12)
        rr = 40 + 150 * ease_out(r_t)
        mx, my = mid, cy
        ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(ov).ellipse([(mx - rr) * S, (my - rr) * S, (mx + rr) * S, (my + rr) * S],
                                   outline=(255, 150, 140, int(170 * (1 - r_t))) if subscribed else accent_hi + (int(170 * (1 - r_t)),), width=4 * S)
        img.alpha_composite(ov)
        d = ImageDraw.Draw(img)

    # --- sparkles from the bell chip when it lights up
    if frame >= BELL_FRAME:
        s_t = seg(frame, BELL_FRAME, BELL_FRAME + 16)
        for k in range(8):
            ang = k * math.pi / 4 + 0.3
            dist = BELL_R + 14 + 46 * ease_out(s_t)
            sx, sy = BELL_X + math.cos(ang) * dist, cy + math.sin(ang) * dist
            sz = 7 * (1 - s_t) + 1
            d.ellipse([(sx - sz) * S, (sy - sz) * S, (sx + sz) * S, (sy + sz) * S], fill=accent_hi + (int(230 * (1 - s_t)),))

    # --- helper line under the card
    line_t = ease_out(seg(frame, *TL["text"]))
    if line_t > 0:
        _text(d, (BOX_W / 2 * S, (338 + (1 - line_t) * 12) * S), "فعّل الجرس ليصلك الجديد", 62 * S,
              _lerp(cream, accent, 0.25) + (int(235 * line_t),), bold=S // 2 or 1)

    img = img.resize((BOX_W, BOX_H), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(img, (BOX_X, BOX_Y))
    return out


def _arrow_points(scale: float) -> list[tuple[float, float]]:
    """A slim, slightly rounded pointer; the tip is at (0, 0)."""
    base = [(0, 0), (0, 40), (10, 31), (17, 46), (25, 42), (18, 28), (31, 28)]
    return [(x * scale, y * scale) for x, y in base]


def touch_layer(brand: dict, frame: int) -> Image.Image | None:
    """Modern pointer arrow: glides to the word "اشترك", presses it, then fades (no old-style cursor)."""
    first = TL["dot"][0]
    if frame < first or frame > TAP_FRAME + 14:
        return None
    start = (W / 2 + 330, 1560)
    target = WORD_CENTER
    t = ease_in_out(seg(frame, first, TAP_FRAME - 2))
    # a slight arc: the pointer swings in from the right and settles on the word
    x = start[0] + (target[0] - start[0]) * t + math.sin(t * math.pi) * 36
    y = start[1] + (target[1] - start[1]) * t
    press = 0.0
    if TAP_FRAME - 3 <= frame <= TAP_FRAME + 5:
        press = math.sin(seg(frame, TAP_FRAME - 3, TAP_FRAME + 5) * math.pi)
    a = seg(frame, first, first + 5) * (1 - seg(frame, TAP_FRAME + 8, TAP_FRAME + 14))
    if a <= 0:
        return None
    S = 3
    size = 200
    scale = (2.1 - 0.24 * press)
    pts = [(px * S + 40 * S, py * S + 40 * S) for px, py in _arrow_points(scale)]
    layer = Image.new("RGBA", (size * S, size * S), (0, 0, 0, 0))
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(px + 5 * S, py + 8 * S) for px, py in pts], fill=(0, 0, 0, int(150 * a)))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(5 * S)))
    d = ImageDraw.Draw(layer)
    outline = (10, 14, 24, int(255 * a))
    d.polygon(pts, fill=(255, 255, 255, int(255 * a)))
    d.line(pts + [pts[0]], fill=outline, width=int(3.2 * S), joint="curve")
    layer = layer.resize((size, size), Image.Resampling.LANCZOS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.alpha_composite(layer, (int(x - 40), int(y - 40)))
    return out


def render_frames(brand: dict, logo: Image.Image, folder: Path) -> None:
    base = background(brand, logo)
    logo_rgb = base.copy()
    for n in range(FRAMES):
        f = base.copy()
        # gentle breathing of the whole logo area (scale 1.0 -> 1.025) and fade-in
        fade = ease_out(seg(n, 0, TL["fade"]))
        if fade < 1:
            dark = Image.new("RGB", (W, H), tuple(brand["bg"]))
            f = Image.blend(dark, f, fade)
        rgba = f.convert("RGBA")
        rgba.alpha_composite(module_layer(brand, n))
        touch = touch_layer(brand, n)
        if touch is not None:
            rgba.alpha_composite(touch)
        rgba.convert("RGB").save(folder / f"f_{n:03d}.png")
    _ = logo_rgb


# ----------------------------------------------------------------------------- sound
def _env_exp(t: np.ndarray, tau: float, attack: float = 0.003) -> np.ndarray:
    return np.minimum(1.0, t / attack) * np.exp(-t / tau)


def _place(track: np.ndarray, sig: np.ndarray, at: float, gain: float, pan: float = 0.0) -> None:
    i = int(at * SR)
    n = min(len(sig), len(track) - i)
    if n <= 0:
        return
    left = gain * (1 - max(0, pan))
    right = gain * (1 + min(0, pan))
    track[i:i + n, 0] += sig[:n] * left
    track[i:i + n, 1] += sig[:n] * right


def tap_sound(rng: np.random.Generator) -> np.ndarray:
    """Soft modern UI tap: a short click, a woody tick and a low thump."""
    t = np.arange(int(0.18 * SR)) / SR
    noise = rng.standard_normal(len(t)) * _env_exp(t, 0.0025, 0.0004)
    noise = noise - np.convolve(noise, np.ones(8) / 8, mode="same")  # keep the highs
    tick = np.sin(2 * np.pi * 2300 * t) * _env_exp(t, 0.012, 0.0006)
    body = np.sin(2 * np.pi * 620 * t) * _env_exp(t, 0.028, 0.0008) * 0.55
    thump = np.sin(2 * np.pi * (130 - 40 * np.minimum(1, t / 0.06)) * t) * _env_exp(t, 0.04, 0.001)
    return 0.40 * noise + 0.55 * tick + 0.45 * body + 0.65 * thump


def chime(freq: float, length: float = 0.9, tau: float = 0.22) -> np.ndarray:
    t = np.arange(int(length * SR)) / SR
    s = np.sin(2 * np.pi * freq * t) + 0.32 * np.sin(2 * np.pi * freq * 2.0 * t) * np.exp(-t / (tau * 0.5)) \
        + 0.12 * np.sin(2 * np.pi * freq * 3.01 * t) * np.exp(-t / (tau * 0.3))
    return s * _env_exp(t, tau, 0.004)


def bell_shimmer(length: float = 0.9) -> np.ndarray:
    t = np.arange(int(length * SR)) / SR
    parts = [(2093, 1.0, 0.34), (3136, 0.62, 0.24), (4186, 0.40, 0.17), (5587, 0.20, 0.10)]
    s = sum(a * np.sin(2 * np.pi * f * t) * np.exp(-t / tau) for f, a, tau in parts)
    return s * np.minimum(1.0, t / 0.002)


def whoosh(rng: np.random.Generator, length: float = 0.5) -> np.ndarray:
    n = int(length * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    # sweep a one-pole low-pass up and down for a soft air movement
    out = np.zeros(n)
    y = 0.0
    for i in range(n):
        a = 0.015 + 0.20 * math.sin(math.pi * i / n) ** 2
        y += a * (noise[i] - y)
        out[i] = y
    out *= np.sin(math.pi * np.arange(n) / n) ** 2
    return out


def build_audio(path: Path) -> dict:
    rng = np.random.default_rng(7)
    track = np.zeros((int(DURATION * SR), 2), np.float64)
    tap_at = TAP_FRAME / FPS
    confirm_at = CONFIRM_FRAME / FPS
    bell_at = BELL_FRAME / FPS
    _place(track, whoosh(rng, 0.35 if TL["tap"] < 25 else 0.5), 0.04 if TL["tap"] < 25 else 0.10, 0.16)
    _place(track, tap_sound(rng), tap_at, 0.70)
    _place(track, chime(1318.5), confirm_at, 0.20, pan=-0.1)
    _place(track, chime(1975.5), confirm_at + 0.085, 0.20, pan=0.1)
    _place(track, bell_shimmer(), bell_at, 0.10)
    _place(track, bell_shimmer(0.7), bell_at + 0.20, 0.06)
    # gentle fade to silence at the very end so the file joins the next clip without a click
    fade_n = int(0.12 * SR)
    track[-fade_n:] *= np.linspace(1, 0, fade_n)[:, None]
    peak = float(np.max(np.abs(track)))
    track *= 0.60 / peak  # about -4.4 dBFS peak, well under the voice's hot peaks
    pcm = (np.clip(track, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    return {"tap_seconds": round(tap_at, 3), "confirm_seconds": round(confirm_at, 3), "bell_seconds": round(bell_at, 3)}


def build_click(path: Path) -> None:
    """The short UI click used by the mid-video like / comment / share / bell chips (mono, 48 kHz)."""
    rng = np.random.default_rng(11)
    sr = 48000
    global SR
    saved, SR = SR, sr
    try:
        track = np.zeros((int(0.42 * sr), 2), np.float64)
        _place(track, tap_sound(rng), 0.0, 0.8)
        _place(track, chime(1568.0, length=0.35, tau=0.12), 0.06, 0.14)
        mono = track.mean(axis=1)
        mono *= 0.85 / float(np.max(np.abs(mono)))
        fade = int(0.05 * sr)
        mono[-fade:] *= np.linspace(1, 0, fade)
        pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
    finally:
        SR = saved


def extract_logo(source: Path, work: Path) -> Image.Image:
    frame = work / "logo_src.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), "-vf", "select=eq(n\\,0)", "-frames:v", "1", str(frame)], check=True)
    return Image.open(frame).convert("RGB").crop((0, 100, W, 800))


def build(source_name: str, out_dir: Path, preview_dir: Path | None) -> dict:
    brand = BRANDS[source_name]
    _use_timeline(source_name)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        logo = extract_logo(ASSET_DIR / source_name, work)
        frames = work / "frames"
        frames.mkdir()
        render_frames(brand, logo, frames)
        wav = work / "audio.wav"
        timing = build_audio(wav)
        out = Path(out_dir) / source_name
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(frames / "f_%03d.png"), "-i", str(wav),
            "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2", "-shortest", "-movflags", "+faststart", str(out),
        ], check=True)
        if preview_dir:
            Path(preview_dir).mkdir(parents=True, exist_ok=True)
            for n in (0, 14, 30, 34, 38, 46, 66):
                Image.open(frames / f"f_{n:03d}.png").resize((270, 480)).save(Path(preview_dir) / f"{source_name[:-4]}_{n:03d}.png")
            (Path(preview_dir) / f"{source_name[:-4]}.wav").write_bytes(wav.read_bytes())
    return {"file": str(out), **timing}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ASSET_DIR))
    parser.add_argument("--preview", default="")
    args = parser.parse_args()
    for name in BRANDS:
        print(build(name, Path(args.out), Path(args.preview) if args.preview else None))
    build_click(Path(args.out) / "ui_click_modern.wav")


if __name__ == "__main__":
    main()
