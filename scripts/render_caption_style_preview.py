from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from clean_v2.podcast_key_text import build_ass as build_sparse_ass
from clean_v2.short_timed_text import build_rich_ass
from clean_v2.visual_cta import VisualCtaEvent, _cta_position, _render as render_cta


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, timeout=120)


def _render_color_frame(*, size: str, ass: Path, at: float, output: Path) -> None:
    # Neutral warm background only so typography, outline, scale and position
    # can be judged without another visual variable.
    duration = max(10.0, at + 1.0)
    _run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=#403A35:s={size}:r=30:d={duration}",
            "-vf", f"ass={ass.resolve()}",
            "-ss", f"{at:.2f}",
            "-frames:v", "1",
            str(output.resolve()),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)

    short_events = [
        {"start": 0.0, "end": 3.0, "text": "لا تنتظر الدافع", "role": "hook"},
        {"start": 3.0, "end": 6.0, "text": "ابدأ بخطوة صغيرة ثم أكمل بهدوء", "role": "beat"},
        {"start": 6.0, "end": 9.0, "text": "خطوة واضحة تصنع الفرق", "role": "payoff"},
    ]
    short_ass = root / "short-preview.ass"
    short_ass.write_text(build_rich_ass(short_events), encoding="utf-8")
    for name, at in (("short-hook.png", 1.2), ("short-body.png", 4.2), ("short-payoff.png", 7.2)):
        _render_color_frame(size="1080x1920", ass=short_ass, at=at, output=root / name)

    film_events = [
        {"start": 0.0, "end": 5.0, "text": "فكرة واحدة تغيّر اتجاه يومك", "role": "hook"},
    ]
    film_ass = root / "film-preview.ass"
    film_ass.write_text(build_sparse_ass(film_events, fmt="film"), encoding="utf-8")
    _render_color_frame(size="1920x1080", ass=film_ass, at=1.0, output=root / "film.png")

    podcast_events = [
        {"start": 0.0, "end": 5.0, "text": "أحيانًا المشكلة ليست فيما تفعله", "role": "hook"},
    ]
    podcast_ass = root / "podcast-preview.ass"
    podcast_ass.write_text(build_sparse_ass(podcast_events, fmt="podcast"), encoding="utf-8")
    _render_color_frame(size="1920x1080", ass=podcast_ass, at=1.0, output=root / "podcast.png")

    # Also render the exact combined Subscribe + Bell asset through the same
    # production CTA renderer, so placement and the restored original colors
    # can be judged from CI rather than from a hand-made mock.
    narration = root / "preview-narration.wav"
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6",
        "-af", "volume=0.08", "-c:a", "pcm_s16le", str(narration.resolve()),
    ])
    for fmt, size in (("short", "1080x1920"), ("film", "1920x1080")):
        base = root / f"cta-{fmt}-base.mp4"
        rendered = root / f"cta-{fmt}.mp4"
        frame = root / f"cta-{fmt}.png"
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=#403A35:s={size}:r=30:d=6",
            "-i", str(narration.resolve()), "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(base.resolve()),
        ])
        x, y = _cta_position(mode="subscribe_combo", fmt=fmt)
        event = VisualCtaEvent(
            mode="subscribe_combo",
            start_seconds=1.0,
            end_seconds=4.5,
            x=x,
            y=y,
            asset="subscribe_bell_reference.mp4",
        )
        render_cta(
            video=base,
            dest=rendered,
            events=[event],
            fmt=fmt,
            narration_path=narration,
        )
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-ss", "2.0", "-i", str(rendered.resolve()),
            "-frames:v", "1", str(frame.resolve()),
        ])


if __name__ == "__main__":
    main()
