"""Full-screen portrait reframing for derived Shorts (Film and Podcast).

The long 16:9 master used to sit as a small strip in the middle of a blurred 9:16 canvas.
The picture now fills 1080x1440 of the 1080x1920 frame (a 3:4 window cut from the 16:9 master),
with only soft blurred bands above and below. The window is centred unless the excerpt's
content clearly sits elsewhere. No new media or provider call: it only re-frames pixels
that already exist in the approved master.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from PIL import Image, ImageFilter

CANVAS_WIDTH, CANVAS_HEIGHT = 1080, 1920
WINDOW_WIDTH, WINDOW_HEIGHT = 1080, 1440  # 3:4 window; 1.33x scale from a 1080p master
WINDOW_TOP = (CANVAS_HEIGHT - WINDOW_HEIGHT) // 2  # 240
EDGE_FADE_PIXELS = 70
SAMPLE_FRAMES = 8
SAMPLE_WIDTH, SAMPLE_HEIGHT = 320, 180
FOCUS_MIN, FOCUS_MAX = 0.25, 0.75  # never slide the window further than a quarter of the free travel
MIN_GAIN = 1.08  # an off-centre window must hold >=8% more detail than the centred one


def fade_mask(destination: Path) -> Path:
    """Greyscale mask: opaque in the middle, easing to transparent at the top and bottom edge."""
    column = Image.new("L", (1, WINDOW_HEIGHT))
    column.putdata([
        255 if edge >= EDGE_FADE_PIXELS else int(255 * (edge / EDGE_FADE_PIXELS) ** 1.4)
        for edge in (min(y, WINDOW_HEIGHT - 1 - y) for y in range(WINDOW_HEIGHT))
    ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    column.resize((WINDOW_WIDTH, WINDOW_HEIGHT), Image.NEAREST).save(destination, "PNG")
    return destination


def column_energy(frames: Sequence[Image.Image]) -> list[float]:
    """Mean edge strength of every pixel column, summed over the sampled frames."""
    totals = [0.0] * SAMPLE_WIDTH
    for frame in frames:
        gray = frame.convert("L").resize((SAMPLE_WIDTH, SAMPLE_HEIGHT))
        edges = gray.filter(ImageFilter.FIND_EDGES)
        columns = edges.resize((SAMPLE_WIDTH, 1), Image.BOX)
        for index, value in enumerate(columns.getdata()):
            totals[index] += float(value)
    return totals


def best_fraction(energy: Sequence[float]) -> float:
    """Horizontal position (0 = left edge, 0.5 = centre, 1 = right edge) of the 3:4 window."""
    # The 3:4 window cut from a 16:9 frame covers 0.75 / (16/9) = 0.421875 of the width.
    window = max(1, round(len(energy) * 0.421875))
    free = len(energy) - window
    if free <= 0 or not any(energy):
        return 0.5
    prefix = [0.0]
    for value in energy:
        prefix.append(prefix[-1] + value)

    def held(offset: int) -> float:
        return prefix[offset + window] - prefix[offset]

    centre = round(free * 0.5)
    low, high = math.ceil(free * FOCUS_MIN), math.floor(free * FOCUS_MAX)
    best = max(range(low, high + 1), key=lambda o: (held(o), -abs(o - centre)))
    if held(centre) > 0 and held(best) < held(centre) * MIN_GAIN:
        return 0.5
    return round(best / free, 4)


def focus_fraction(
    source: Path, start: float, duration: float,
    *, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> float:
    """Sample the excerpt and return the window position; any failure means the centre."""
    try:
        rate = SAMPLE_FRAMES / max(1.0, float(duration))
        completed = run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{float(start):.3f}", "-t", f"{float(duration):.3f}",
             "-i", str(source), "-vf", f"fps={rate:.5f},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:flags=area,format=gray",
             "-frames:v", str(SAMPLE_FRAMES), "-f", "rawvideo", "-"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        )
        size = SAMPLE_WIDTH * SAMPLE_HEIGHT
        data = completed.stdout
        frames = [Image.frombytes("L", (SAMPLE_WIDTH, SAMPLE_HEIGHT), data[i * size:(i + 1) * size])
                  for i in range(len(data) // size)]
        if not frames:
            return 0.5
        return best_fraction(column_energy(frames))
    except Exception:
        return 0.5


def filter_graph(fraction: float) -> str:
    """ffmpeg filter graph: blurred full-canvas background plus the faded 3:4 picture window."""
    fraction = min(1.0, max(0.0, float(fraction)))
    return (
        "[0:v]split=2[bg0][fg0];"
        f"[bg0]scale={CANVAS_WIDTH}:{CANVAS_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_WIDTH}:{CANVAS_HEIGHT},boxblur=26:3,eq=brightness=-0.22:saturation=0.9[bg];"
        f"[fg0]crop=w=ih*3/4:h=ih:x='(iw-ow)*{fraction:.4f}':y=0,scale={WINDOW_WIDTH}:{WINDOW_HEIGHT},"
        "format=yuva420p[fgc];"
        "[fgc][1:v]alphamerge[fg];"
        f"[bg][fg]overlay=0:{WINDOW_TOP}:format=auto:shortest=1,format=yuv420p[v]"
    )
