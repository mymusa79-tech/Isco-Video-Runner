from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

PRAYER_SENTENCE = "اللهم صلِّ وسلِّم على نبينا محمد."
SHORT_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح."
LONG_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أصدق، ونبحث عن خطوة عملية نحو حياة أوضح."
PODCAST_CHANNEL_DEFINITION = "بودكاست من نداء اليقظة"

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "identity"
_SHORT_INTRO = _ASSET_DIR / "short_intro.mp4"
_SHORT_OUTRO = _ASSET_DIR / "short_outro.mp4"
_LONG_INTRO = _ASSET_DIR / "long_intro.mp4"
_LONG_OUTRO = _ASSET_DIR / "long_outro.mp4"
_PODCAST_LOGO_B64 = _ASSET_DIR / "podcast_logo_v8.b64"
_PRAYER_IMAGE = _ASSET_DIR / "prayer_image.jpg"
_SENTENCE_END_RE = re.compile(r"[.!؟!]")
_SUPPORTED_IDENTITY_FORMATS = frozenset({"short", "film", "podcast"})
_TIMING_PROFILES = {
    # Structural breathing is deliberate and provider-independent. It is inserted
    # only at major opening boundaries, never inside ordinary Gemini speech.
    # Long-form gets slightly more room than Shorts without becoming sluggish.
    "short": {
        "post_hook_silence_seconds": 1.15,
        "intro_silence_seconds": 1.15,
        "post_prayer_silence_seconds": 0.35,
        "pre_topic_silence_seconds": 0.65,
        "final_silence_seconds": 2.20,
    },
    "film": {
        "post_hook_silence_seconds": 1.15,
        "intro_silence_seconds": 2.20,
        "post_prayer_silence_seconds": 0.45,
        "pre_topic_silence_seconds": 0.85,
        "final_silence_seconds": 3.50,
    },
    "podcast": {
        # Outside the Text has its own deliberate podcast rhythm:
        # listener A asks -> breath -> 6s branded intro -> prayer -> breath ->
        # Charon answers directly in the topic. Body music starts with that answer.
        "post_hook_silence_seconds": 0.75,
        "intro_silence_seconds": 6.00,
        "post_prayer_silence_seconds": 0.65,
        # Kept in the shared profile schema but intentionally unused for podcast:
        # there is no extra spoken channel-definition beat after the prayer.
        "pre_topic_silence_seconds": 0.00,
        "final_silence_seconds": 6.50,
    },
}


def identity_timing_profile(fmt: str) -> dict[str, float]:
    if fmt not in _SUPPORTED_IDENTITY_FORMATS:
        raise RuntimeError(f"identity timing unsupported format: {fmt}")
    return dict(_TIMING_PROFILES[fmt])


def _podcast_text_image(text: str, *, size: int, fill: tuple[int, int, int, int]):
    from PIL import Image, ImageDraw, ImageFont

    font_paths = (
        "/usr/share/fonts/truetype/noto/NotoKufiArabic-SemiBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-SemiBold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    )
    font_path = next((item for item in font_paths if Path(item).is_file()), None)
    if font_path is None:
        raise RuntimeError("podcast_v8_arabic_font_missing")
    font = ImageFont.truetype(font_path, size)
    probe = Image.new("RGBA", (1400, 220), (0, 0, 0, 0))
    draw = ImageDraw.Draw(probe)
    bbox = draw.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
    image = Image.new("RGBA", (bbox[2] - bbox[0] + 56, bbox[3] - bbox[1] + 44), (0, 0, 0, 0))
    ImageDraw.Draw(image).text(
        (28, -bbox[1] + 12),
        text,
        font=font,
        fill=fill,
        direction="rtl",
        language="ar",
    )
    return image


def _build_podcast_v8_card(*, logo, kind: str):
    from PIL import Image, ImageDraw, ImageFilter

    width, height = 1280, 720
    image = Image.new("RGBA", (width, height), (11, 12, 13, 255))
    halo = Image.new("RGBA", image.size, (0, 0, 0, 0))
    halo_draw = ImageDraw.Draw(halo)
    cy = 300 if kind == "outro" else 320
    halo_draw.ellipse((450, cy - 190, 830, cy + 190), fill=(203, 157, 75, 15))
    image.alpha_composite(halo.filter(ImageFilter.GaussianBlur(80)))

    target_logo_height = 430
    scale = target_logo_height / max(1, logo.height)
    logo_scaled = logo.resize(
        (max(1, int(logo.width * scale)), target_logo_height),
        Image.Resampling.LANCZOS,
    )
    logo_y = 90 if kind == "intro" else 70
    image.alpha_composite(logo_scaled, ((width - logo_scaled.width) // 2, logo_y))

    if kind == "intro":
        subtitle = _podcast_text_image(
            "بودكاست من نداء اليقظة",
            size=28,
            fill=(236, 232, 222, 255),
        )
        image.alpha_composite(subtitle, ((width - subtitle.width) // 2, 575))
    else:
        tagline = _podcast_text_image(
            "نلتقي خارج النص.",
            size=38,
            fill=(221, 171, 88, 255),
        )
        parent = _podcast_text_image(
            "من نداء اليقظة",
            size=23,
            fill=(150, 146, 138, 255),
        )
        image.alpha_composite(tagline, ((width - tagline.width) // 2, 555))
        image.alpha_composite(parent, ((width - parent.width) // 2, 625))
    return image


def _render_podcast_v8_asset(*, card: Path, destination: Path, duration: float, kind: str) -> Path:
    if kind == "intro":
        tones = (
            ("66", "0.72", "0.95", "1.20"),
            ("132", "0.72", "0.70", "0.30"),
            ("392", "1.05", "1.70", "0.50"),
            ("784", "1.05", "1.25", "0.18"),
        )
    else:
        tones = (
            ("61", "0.62", "0.90", "1.00"),
            ("329.63", "0.82", "1.70", "0.45"),
            ("659.26", "0.82", "1.20", "0.14"),
            ("125", "4.70", "0.82", "0.24"),
        )

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-framerate", "24", "-i", str(card),
    ]
    filters: list[str] = []
    labels: list[str] = []
    for index, (frequency, start, tone_duration, gain) in enumerate(tones, start=1):
        command.extend(
            [
                "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration={tone_duration}",
            ]
        )
        delay_ms = int(round(float(start) * 1000))
        label = f"a{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:a]volume={gain},afade=t=out:st={max(0.05, float(tone_duration) * 0.30):.3f}:"
            f"d={max(0.08, float(tone_duration) * 0.70):.3f},"
            f"adelay={delay_ms}:all=1[{label}]"
        )
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
        + f"apad=pad_dur={duration:.3f},atrim=duration={duration:.3f},"
        + "alimiter=limit=0.20:level=disabled[aout]"
    )
    fade_out = max(0.0, duration - 0.70)
    video_filter = (
        f"scale=1280:720,format=yuv420p,"
        f"fade=t=in:st=0:d=0.45,fade=t=out:st={fade_out:.3f}:d=0.70"
    )
    command.extend(
        [
            "-filter_complex", ";".join(filters),
            "-vf", video_filter,
            "-map", "0:v:0", "-map", "[aout]",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k", "-ac", "2",
            "-movflags", "+faststart", str(destination),
        ]
    )
    subprocess.run(
        command,
        check=True,
        timeout=120,
        env={
            key: value
            for key, value in os.environ.items()
            if "TOKEN" not in key.upper() and "API_KEY" not in key.upper()
        },
    )
    if not destination.is_file() or destination.stat().st_size <= 1024:
        raise RuntimeError(f"podcast_v8_asset_generation_failed:{kind}")
    return destination


def _ensure_podcast_v8_assets(runtime_dir: Path) -> tuple[Path, Path]:
    from PIL import Image

    root = Path(runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    intro = root / "podcast_intro_v8.mp4"
    outro = root / "podcast_outro_v8.mp4"
    if intro.is_file() and intro.stat().st_size > 1024 and outro.is_file() and outro.stat().st_size > 1024:
        return intro, outro
    try:
        encoded = "".join(_PODCAST_LOGO_B64.read_text(encoding="ascii").split())
        logo_bytes = base64.b64decode(encoded, validate=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError("podcast_v8_logo_source_invalid") from exc

    logo_path = root / "podcast_logo_v8.png"
    logo_path.write_bytes(logo_bytes)
    logo = Image.open(logo_path).convert("RGBA")
    intro_card = root / "podcast_intro_v8.png"
    outro_card = root / "podcast_outro_v8.png"
    _build_podcast_v8_card(logo=logo, kind="intro").convert("RGB").save(intro_card, quality=95)
    _build_podcast_v8_card(logo=logo, kind="outro").convert("RGB").save(outro_card, quality=95)
    try:
        _render_podcast_v8_asset(card=intro_card, destination=intro, duration=6.00, kind="intro")
        _render_podcast_v8_asset(card=outro_card, destination=outro, duration=6.50, kind="outro")
    finally:
        logo_path.unlink(missing_ok=True)
        intro_card.unlink(missing_ok=True)
        outro_card.unlink(missing_ok=True)
    return intro, outro


def identity_asset_paths(
    fmt: str,
    *,
    runtime_dir: Path | None = None,
) -> dict[str, Path]:
    """Return the approved per-format identity with no provider call or paid dependency."""
    if fmt == "podcast":
        intro, outro = _ensure_podcast_v8_assets(
            Path(runtime_dir or (Path.cwd() / ".clean-v2-identity-assets"))
        )
    else:
        intro, outro, _width, _height = _asset_pair(fmt)
    return {"intro": intro, "prayer": _PRAYER_IMAGE, "outro": outro}


def _first_sentence(text: str) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    match = _SENTENCE_END_RE.search(compact)
    return compact[: match.end()].strip() if match else compact


def channel_definition(fmt: str, opener: str = "") -> str:
    if fmt == "short":
        return SHORT_CHANNEL_DEFINITION
    if fmt == "podcast":
        # Visual V8 intro owns the series/channel branding. This value remains
        # available as metadata but is intentionally not inserted into speech.
        return PODCAST_CHANNEL_DEFINITION
    candidate = " ".join(str(opener or "").split()).strip()
    return candidate or LONG_CHANNEL_DEFINITION


def inject_spoken_identity(
    sections: list[dict[str, Any]],
    *,
    fmt: str,
    opener: str = "",
    closer: str = "",
) -> None:
    """Keep the approved spoken order: hook -> prayer -> channel definition -> topic.

    Visual identity is timed later from measured voice-unit boundaries inside Timeline First;
    nothing is appended after the final render or allowed to extend narration duration.
    """
    if fmt not in {"short", "film", "podcast"} or not sections:
        return

    definition = channel_definition(fmt, opener)
    identity_block = (
        PRAYER_SENTENCE
        if fmt == "podcast"
        else f"{PRAYER_SENTENCE} {definition}".strip()
    )
    closer = " ".join(str(closer or "").split()).strip()

    for section in sections:
        narration = " ".join(str(section.get("narration") or "").split()).strip()
        for phrase in (PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, LONG_CHANNEL_DEFINITION, definition, closer):
            if phrase:
                narration = " ".join(narration.replace(phrase, " ").split()).strip()
        section["narration"] = narration

    first = str(sections[0].get("narration") or "").strip()
    hook = _first_sentence(first)
    if not hook:
        raise RuntimeError("identity sequence requires a non-empty first-sentence hook")
    remainder = first[len(hook):].lstrip()
    # Keep a hard sentence boundary before the host-owned prayer. Providers
    # sometimes return a valid hook without terminal punctuation; without this
    # boundary the prayer's final period becomes the first sentence terminator,
    # so the downstream invariant incorrectly treats hook+prayer as one sentence.
    if not _SENTENCE_END_RE.search(hook[-1:]):
        hook = f"{hook}."
    sections[0]["narration"] = f"{hook} {identity_block} {remainder}".strip()

    if fmt in {"film", "podcast"} and closer:
        sections[-1]["narration"] = (
            f"{str(sections[-1].get('narration') or '').rstrip()} {closer}"
        ).strip()


def assert_spoken_identity(
    sections: list[dict[str, Any]],
    *,
    fmt: str,
    opener: str = "",
    closer: str = "",
) -> None:
    if fmt not in {"short", "film", "podcast"} or not sections:
        return

    definition = channel_definition(fmt, opener)
    joined = "\n".join(str(item.get("narration") or "") for item in sections)

    # Backward-compatible seam for direct repair/unit fixtures that exercise the
    # older opener/closer contract without running the production identity injector.
    # Real pipeline scripts always contain PRAYER_SENTENCE before this invariant.
    if PRAYER_SENTENCE not in joined:
        legacy_opener = " ".join(str(opener or "").split()).strip()
        legacy_closer = " ".join(str(closer or "").split()).strip()
        if legacy_opener and joined.count(legacy_opener) != 1:
            raise RuntimeError("legacy identity requires exactly one opener")
        if fmt in {"film", "podcast"} and legacy_closer and joined.count(legacy_closer) != 1:
            raise RuntimeError("legacy identity requires exactly one closer")
        return

    if joined.count(PRAYER_SENTENCE) != 1:
        raise RuntimeError("identity sequence requires exactly one approved prayer sentence")
    if fmt != "podcast" and joined.count(definition) != 1:
        raise RuntimeError("identity sequence requires exactly one channel-definition sentence")

    first = str(sections[0].get("narration") or "")
    hook = _first_sentence(first)
    prayer_pos = first.find(PRAYER_SENTENCE)
    if not hook or prayer_pos < len(hook):
        raise RuntimeError("identity sequence order must begin hook -> prayer")
    if fmt != "podcast":
        definition_pos = first.find(definition)
        if definition_pos <= prayer_pos:
            raise RuntimeError("identity sequence order must be hook -> prayer -> channel definition")

    if fmt in {"film", "podcast"}:
        closer = " ".join(str(closer or "").split()).strip()
        if closer and joined.count(closer) != 1:
            raise RuntimeError("identity sequence requires exactly one long-form closer")


def _asset_pair(fmt: str) -> tuple[Path, Path, int, int]:
    if fmt == "short":
        return _SHORT_INTRO, _SHORT_OUTRO, 1080, 1920
    if fmt == "film":
        return _LONG_INTRO, _LONG_OUTRO, 1920, 1080
    if fmt == "podcast":
        # Podcast assets are generated locally from the approved V8 logo source.
        return Path("podcast_intro_v8.mp4"), Path("podcast_outro_v8.mp4"), 1920, 1080
    raise RuntimeError(f"identity media unsupported format: {fmt}")
