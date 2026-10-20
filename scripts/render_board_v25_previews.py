"""Offline visual-review contact frames for the opt-in Board V25 theme.

Run on the PR only; screenshots are evidence for a human review, not automated approval.
No model API or external assets. Does not alter the production renderer.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from clean_v2 import board_scene


SAMPLES = (
    (
        "film_compare", "film", "compare", "الافتراض والواقع",
        ("ما نتوقعه", "ما يحدث فعلًا"),
        ("حين نظن أن البداية صعبة", "عندما نرى أول فعل ملموس"),
    ),
    (
        "film_row", "film", "row", "من الفكرة إلى التجربة",
        ("افتراض", "اختبار", "نتيجة"),
        ("نكتب الافتراض", "ثم نختبره", "وأخيرًا نراجع النتيجة"),
    ),
    (
        "podcast_steps", "podcast", "steps", "قرارات لا وعود",
        ("نسمّي المشكلة", "نقبل الحقيقة", "نختار فعلًا"),
        ("أولًا نسمّي المشكلة", "ثم نقبل الحقيقة", "وأخيرًا نختار فعلًا"),
    ),
    (
        "short_grid", "short", "grid", "ثلاثة مصادر للتشتيت",
        ("التنبيهات", "المقاطعات", "تعدد المهام"),
        ("تبدأ التنبيهات", "ثم تأتي المقاطعات", "وتزداد المهام المتزامنة"),
    ),
)


def create_samples(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name, fmt, layout, title, labels, triggers in SAMPLES:
        items = [{"label": label, "at_text": trigger}
                 for label, trigger in zip(labels, triggers, strict=True)]
        text = "، ".join(triggers) + "، ".join(triggers)
        spec = {"layout": layout, "theme": "editorial", "title": title, "items": items}
        video = output / f"{name}.mp4"
        frame = output / f"{name}.png"
        mobile = output / f"{name}_mobile.png"
        seconds = 5.0
        board_scene.render_board_clip(
            spec, output, video, seconds=seconds,
            times=board_scene.reveal_times(text, items, seconds),
            fmt=fmt,
        )
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "4.6",
             "-i", str(video), "-frames:v", "1", str(frame)], check=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(frame),
             "-vf", "scale=360:-1", str(mobile)], check=True)
        video.unlink()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=Path("board-v25-samples"))
    create_samples(p.parse_args().output)
