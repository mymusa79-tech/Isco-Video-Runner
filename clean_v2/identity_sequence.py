from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

PRAYER_SENTENCE = "اللهم صلِّ وسلِّم على نبينا محمد."
SHORT_CHANNEL_DEFINITION = "هنا نداء اليقظة؛ وعيٌ أوضح للحياة اليومية."
SHORT_CHANNEL_DEFINITION_LEGACY = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أوضح."
LONG_CHANNEL_DEFINITION = "وهنا في نداء اليقظة، نقترب من أفكار الحياة اليومية بوعيٍ أصدق، ونبحث عن خطوة عملية نحو حياة أوضح."
PODCAST_CHANNEL_DEFINITION = "بودكاست من نداء اليقظة"
# Podcast only: the Writer authors a short, topic-specific bridge that leads into the
# prayer ("B: <bridge>. [[PRAYER]] B: <answer...>"). The host owns the prayer text itself
# and replaces this marker with it. Used only when it sits exactly where a bridge belongs;
# otherwise the marker is stripped and a neutral fallback bridge is used instead.
PODCAST_PRAYER_MARKER = "[[PRAYER]]"
PODCAST_FALLBACK_BRIDGE = "سأجيبك على هذا، لكن أولاً."
_PODCAST_MARKER_RE = re.compile(r"\s*\[\[PRAYER\]\]\s*")
_PODCAST_BRIDGE_TURN_RE = re.compile(r"^B:\s*(?P<bridge>\S.*?)\s*$", re.S)
_PODCAST_BRIDGE_MAX_WORDS = 14

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "identity"
_SHORT_INTRO = _ASSET_DIR / "short_intro.mp4"
_SHORT_OUTRO = _ASSET_DIR / "short_outro.mp4"
_ALA_MAHL_OUTRO = _ASSET_DIR / "ala_mahl_outro.mp4"
# The owner's approved 67-frame outro plays once at its original 30 fps.
ALA_MAHL_OUTRO_SECONDS = 67 / 30
_LONG_INTRO = _ASSET_DIR / "long_intro.mp4"
_LONG_OUTRO = _ASSET_DIR / "long_outro.mp4"
_PODCAST_LOGO_B64 = _ASSET_DIR / "podcast_logo_v8.b64"
_PRAYER_IMAGE = _ASSET_DIR / "prayer_image.jpg"
_ALA_MAHL_APPROVED_DIR = _ASSET_DIR / "ala_mahl_podcast_approved"
_SENTENCE_END_RE = re.compile(r"[.!؟!]")
_SUPPORTED_IDENTITY_FORMATS = frozenset({"short", "film", "podcast"})
_TIMING_PROFILES = {
    # Structural breathing is deliberate and provider-independent. It is inserted
    # only at major opening boundaries, never inside ordinary Gemini speech.
    # Long-form gets slightly more room than Shorts without becoming sluggish.
    "short": {
        # Hook -> natural breath -> prayer -> spoken handoff with its logo -> topic.
        # There is no separate silent logo intro.
        "post_hook_silence_seconds": 0.25,
        "intro_silence_seconds": 0.00,
        "post_prayer_silence_seconds": 0.25,
        "pre_topic_silence_seconds": 0.00,
        # ~0.45 s of quiet so the last sentence lands, then the full 67-frame outro.
        "final_silence_seconds": 2.7,
    },
    "film": {
        # Film has no separate branded intro: the spoken definition owns the logo.
        "post_hook_silence_seconds": 0.35,
        "intro_silence_seconds": 0.00,
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


def identity_timing_profile(fmt: str, *, template: str = "") -> dict[str, float]:
    if fmt not in _SUPPORTED_IDENTITY_FORMATS:
        raise RuntimeError(f"identity timing unsupported format: {fmt}")
    profile = dict(_TIMING_PROFILES[fmt])
    if fmt == "short" and template == "ala_mahl":
        profile["final_silence_seconds"] = ALA_MAHL_OUTRO_SECONDS
    return profile


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


def _build_ala_mahl_podcast_card(*, kind: str):
    """Own 16:9 identity, deliberately free of Nidaa/Outside the Text marks."""
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.new("RGBA", (1280, 720), (30, 29, 28, 255))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((370, -75, 910, 465), fill=(196, 174, 137, 29))
    image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(85)))
    draw = ImageDraw.Draw(image)
    draw.ellipse((470, 72, 810, 412), outline=(194, 168, 130, 255), width=5)
    draw.ellipse((483, 85, 797, 399), outline=(132, 116, 94, 155), width=1)

    def center_text(label: str, font_size: int, y: int, fill: tuple[int, int, int, int]) -> None:
        lettering = _podcast_text_image(label, size=font_size, fill=fill)
        image.alpha_composite(lettering, ((1280 - lettering.width) // 2, y))

    center_text("على مهل", 78, 193, (244, 235, 217, 255))
    if kind == "intro":
        center_text("بودكاست على مهل", 42, 469, (235, 223, 203, 255))
        center_text("المواقف من أكثر من وجه", 29, 552, (176, 161, 142, 255))
    else:
        center_text("نلتقي على مهل.", 45, 472, (235, 223, 203, 255))
        center_text("حكاية وحوار وفهم", 28, 556, (176, 161, 142, 255))
    return image


def _approved_ala_mahl_image(kind: str) -> Path | None:
    """Owner-supplied real circular identity; never fall through on malformed assets."""
    if kind not in {"intro", "prayer", "outro"}:
        raise ValueError("unsupported_ala_mahl_identity_kind")
    path = _ALA_MAHL_APPROVED_DIR / f"{kind}_16x9.png"
    if not path.exists():
        return None
    from PIL import Image
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        if image.size != (1920, 1080) or image.format != "PNG":
            raise RuntimeError(f"ala_mahl_approved_identity_invalid:{kind}")
    return path


_ALA_MAHL_VIDEO_SECONDS = {"intro": 6.0, "outro": 6.5}
_ALA_MAHL_VIDEO_SIZES = frozenset({(1280, 720), (1920, 1080)})
_ALA_MAHL_VIDEO_MAX_BYTES = 20 * 1024 * 1024


def _approved_ala_mahl_video(kind: str) -> Path | None:
    """Owner-approved *animated* intro/outro for the new podcast only.

    A present but malformed file fails loudly (never a silent fallback), exactly like
    the approved still images. Absent means "not installed", not "rejected".
    """
    if kind not in _ALA_MAHL_VIDEO_SECONDS:
        raise ValueError("unsupported_ala_mahl_identity_video_kind")
    path = _ALA_MAHL_APPROVED_DIR / f"{kind}_16x9.mp4"
    if not path.exists():
        return None
    invalid = RuntimeError(f"ala_mahl_approved_identity_video_invalid:{kind}")
    try:
        size = path.stat().st_size
        if not 1024 < size <= _ALA_MAHL_VIDEO_MAX_BYTES:
            raise invalid
        run = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height",
             "-of", "json", str(path)],
            text=True, capture_output=True, check=True, timeout=30,
        )
        details = json.loads(run.stdout)
        streams = details.get("streams") or []
        video = next((row for row in streams if row.get("codec_type") == "video"), None)
        has_audio = any(row.get("codec_type") == "audio" for row in streams)
        duration = float((details.get("format") or {}).get("duration") or 0.0)
    except RuntimeError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
        raise invalid from exc
    if (video is None or not has_audio
            or (video.get("width"), video.get("height")) not in _ALA_MAHL_VIDEO_SIZES
            or abs(duration - _ALA_MAHL_VIDEO_SECONDS[kind]) > 0.15):
        raise invalid
    return path


def _ensure_ala_mahl_podcast_assets(runtime_dir: Path) -> tuple[Path, Path]:
    """Prefer approved owner's animated video, then approved stills, then the prototype.

    Never touches Outside the Text V8.
    """
    root = Path(runtime_dir)
    root.mkdir(parents=True, exist_ok=True)
    destinations = {}
    for kind, duration in (("intro", 6.0), ("outro", 6.5)):
        video = _approved_ala_mahl_video(kind)
        if video is not None:
            destinations[kind] = video
            continue
        original = _approved_ala_mahl_image(kind)
        # Filename hash prevents stale pre-approval cards returning from resume cache.
        fingerprint = (hashlib.sha256(original.read_bytes()).hexdigest()[:12]
                       if original is not None else "prototype_v1")
        destination = root / f"ala_mahl_podcast_{kind}_{fingerprint}.mp4"
        destinations[kind] = destination
        if destination.is_file() and destination.stat().st_size > 1024:
            continue
        if original is not None:
            _render_podcast_v8_asset(card=original, destination=destination,
                                     duration=duration, kind=kind)
        else:
            # Backward-compatible fallback only while owner media is not installed.
            card = root / f"ala_mahl_podcast_{kind}_prototype_v1.jpg"
            try:
                _build_ala_mahl_podcast_card(kind=kind).convert("RGB").save(card, quality=95)
                _render_podcast_v8_asset(card=card, destination=destination,
                                         duration=duration, kind=kind)
            finally:
                card.unlink(missing_ok=True)
    return destinations["intro"], destinations["outro"]


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
    template: str = "",
) -> dict[str, Path]:
    """Return the approved per-format identity with no provider call or paid dependency."""
    if fmt == "podcast":
        identity_root = Path(runtime_dir or (Path.cwd() / ".clean-v2-identity-assets"))
        intro, outro = (
            _ensure_ala_mahl_podcast_assets(identity_root)
            if template == "ala_mahl_dialogue"
            else _ensure_podcast_v8_assets(identity_root)
        )
    else:
        intro, outro, _width, _height = _asset_pair(fmt)
    if fmt == "short" and template == "ala_mahl":
        outro = _ALA_MAHL_OUTRO
    prayer = (_approved_ala_mahl_image("prayer") or _PRAYER_IMAGE
              if fmt == "podcast" and template == "ala_mahl_dialogue"
              else _PRAYER_IMAGE)
    return {"intro": intro, "prayer": prayer, "outro": outro}


def _first_sentence(text: str) -> str:
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    match = _SENTENCE_END_RE.search(compact)
    return compact[: match.end()].strip() if match else compact


def _short_opening_hook(text: str) -> str:
    """Owner order (27 Oct 2026): the Short prayer waits for the FIRST TWO sentences.

    One sentence was too little to say what the Short is about before the prayer. The hook
    is now up to two complete sentences of the first section; a first section with a single
    sentence keeps that sentence (the prayer never moves into the next section).
    """
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(compact)]
    if len(ends) >= 2:
        return compact[: ends[1]].strip()
    return _first_sentence(compact)


FILM_HOOK_MIN_WORDS = 50
FILM_HOOK_MAX_SENTENCES = 4


def _film_opening_hook(text: str) -> str:
    """Owner order (29 Oct 2026): the film prayer waits for the whole hook.

    NIDAA_FILM_HOOKS_V1 puts the explicit promise (hypothesis, test, when it fails) inside
    the hook, about 40-60 spoken words, BEFORE the prayer and the channel definition. The
    hook is therefore whole sentences from the first section until at least
    FILM_HOOK_MIN_WORDS words are reached, never more than FILM_HOOK_MAX_SENTENCES
    sentences; a first section with fewer sentences keeps what it has.
    """
    compact = " ".join(str(text or "").split()).strip()
    if not compact:
        return ""
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(compact)]
    if not ends:
        return compact
    for count, end in enumerate(ends[:FILM_HOOK_MAX_SENTENCES], start=1):
        if len(compact[:end].split()) >= FILM_HOOK_MIN_WORDS:
            return compact[:end].strip()
    return compact[: ends[min(len(ends), FILM_HOOK_MAX_SENTENCES) - 1]].strip()


def _opening_hook_for(fmt: str, text: str) -> str:
    if fmt == "podcast":
        return _podcast_opening_hook(text)
    if fmt == "short":
        return _short_opening_hook(text)
    if fmt == "film":
        return _film_opening_hook(text)
    return _first_sentence(text)


def _podcast_first_a_turn(text: str) -> str:
    """Keep the complete first A turn (including internal full stops).

    Legacy unlabeled openings retain the first-sentence boundary. This makes
    the podcast identity independent of punctuation inside the listener's story.
    """
    compact = " ".join(str(text or "").split()).strip()
    if not compact.startswith("A:"):
        return _first_sentence(compact)
    boundaries = [
        marker.start() for marker in (
            re.search(r"(?<!\S)B:\s+", compact),
            re.search(re.escape(PRAYER_SENTENCE), compact),
            re.search(re.escape(PODCAST_PRAYER_MARKER), compact),
        ) if marker is not None and marker.start() > 2
    ]
    return compact[:min(boundaries)].strip() if boundaries else compact


def _podcast_opening_hook(text: str) -> str:
    """Owner order (27 Oct 2026): A asks, B's first reply lands, THEN the branded intro.

    The opening unit is the complete first A turn plus the host's first B turn, so the
    listener hears a real exchange before the intro card and the prayer. The B turn ends
    at the next A label (or at the prayer / marker if one already sits there). Scripts
    where B does not directly follow A keep the previous A-only boundary.
    """
    first = _podcast_first_a_turn(text)
    compact = " ".join(str(text or "").split()).strip()
    if not compact.startswith("A:"):
        return first
    rest = compact[len(first):].lstrip()
    if not re.match(r"B:\s+\S", rest):
        return first
    boundaries = [
        marker.start() for marker in (
            re.search(r"(?<!\S)A:\s+", rest),
            re.search(re.escape(PRAYER_SENTENCE), rest),
            re.search(re.escape(PODCAST_PRAYER_MARKER), rest),
        ) if marker is not None and marker.start() > 2
    ]
    b_turn = rest[:min(boundaries)].strip() if boundaries else rest
    return f"{first} {b_turn}".strip()


def channel_definition(fmt: str, opener: str = "") -> str:
    if fmt == "short":
        # Owner decision (Oct 2026): Shorts carry only the prayer and the outro. There is no
        # spoken channel definition and no logo card, so nothing is inserted between them.
        return ""
    if fmt == "podcast":
        # Visual V8 intro owns the series/channel branding. This value remains
        # available as metadata but is intentionally not inserted into speech.
        return PODCAST_CHANNEL_DEFINITION
    candidate = " ".join(str(opener or "").split()).strip()
    return candidate or LONG_CHANNEL_DEFINITION


def _podcast_marker_prayer_in_place(sections: list[dict[str, Any]]) -> bool:
    """Validate the Writer's [[PRAYER]] marker placement; strip every marker if misplaced.

    Well placed = exactly one marker in the whole script, in section 1, after the hook
    sentence, preceded by one short B turn (the bridge) with no question mark. Any other
    placement strips every marker and reports False so the caller falls back safely.
    """
    total = sum(str(item.get("narration") or "").count(PODCAST_PRAYER_MARKER) for item in sections)
    placed = False
    if total == 1:
        first = " ".join(str(sections[0].get("narration") or "").split()).strip()
        position = first.find(PODCAST_PRAYER_MARKER)
        hook = _podcast_first_a_turn(first)
        if position >= len(hook) > 0:
            between = first[len(hook):position].strip()
            match = _PODCAST_BRIDGE_TURN_RE.match(between)
            bridge = match.group("bridge") if match else ""
            words = len(bridge.split())
            if (
                2 <= words <= _PODCAST_BRIDGE_MAX_WORDS
                and "؟" not in bridge
                and "?" not in bridge
                and "A:" not in bridge
                and "B:" not in bridge
                and PRAYER_SENTENCE not in bridge
                and bridge[-1:] in {".", "،", ":", "!", "…"}
            ):
                # Keep the marker for now: the generic strip loop in the caller removes any
                # earlier prayer text, and the marker is swapped for the prayer after it.
                placed = True
    if not placed:
        for item in sections:
            item["narration"] = _PODCAST_MARKER_RE.sub(
                " ", str(item.get("narration") or "")
            ).strip()
    return placed


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

    bridge_in_place = False
    if fmt == "podcast":
        # Marker first: it is not a phrase to strip, it carries the bridge placement.
        bridge_in_place = _podcast_marker_prayer_in_place(sections)

    for section in sections:
        narration = " ".join(str(section.get("narration") or "").split()).strip()
        for phrase in (PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, SHORT_CHANNEL_DEFINITION_LEGACY, LONG_CHANNEL_DEFINITION, definition, closer):
            if phrase:
                narration = " ".join(narration.replace(phrase, " ").split()).strip()
        section["narration"] = narration

    first = str(sections[0].get("narration") or "").strip()
    hook = _opening_hook_for(fmt, first)
    if not hook:
        raise RuntimeError("identity sequence requires a non-empty opening hook")
    if bridge_in_place:
        # The Writer's bridge already leads into the marker; swap it for the host-owned
        # prayer exactly once (the strip loop above removed any earlier prayer text).
        if first.count(PODCAST_PRAYER_MARKER) != 1:
            raise RuntimeError("podcast bridge placement lost its prayer marker")
        sections[0]["narration"] = " ".join(
            first.replace(PODCAST_PRAYER_MARKER, f" {PRAYER_SENTENCE} ").split()
        ).strip()
        _finish_closer(sections, fmt, closer)
        return
    remainder = first[len(hook):].lstrip()
    if fmt == "podcast" and remainder.startswith("B:"):
        # No usable Writer bridge: keep a neutral spoken lead-in so the prayer never
        # arrives abruptly right after the question.
        identity_block = f"B: {PODCAST_FALLBACK_BRIDGE} {PRAYER_SENTENCE}"
    # Keep a hard sentence boundary before the host-owned prayer. Providers
    # sometimes return a valid hook without terminal punctuation; without this
    # boundary the prayer's final period becomes the first sentence terminator,
    # so the downstream invariant incorrectly treats hook+prayer as one sentence.
    if not _SENTENCE_END_RE.search(hook[-1:]):
        hook = f"{hook}."
    sections[0]["narration"] = f"{hook} {identity_block} {remainder}".strip()
    _finish_closer(sections, fmt, closer)


def _finish_closer(sections: list[dict[str, Any]], fmt: str, closer: str) -> None:
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
    if fmt != "podcast" and definition and joined.count(definition) != 1:
        raise RuntimeError("identity sequence requires exactly one channel-definition sentence")

    first = str(sections[0].get("narration") or "")
    # The prayer is host-owned text; the Short hook is measured on the script WITHOUT it.
    if fmt == "short":
        hook_source = first.replace(PRAYER_SENTENCE, " ")
    elif fmt == "film":
        # A multi-sentence film hook must be measured on the script WITHOUT the
        # host-owned prayer and channel definition, or they would count as hook sentences.
        hook_source = first.replace(PRAYER_SENTENCE, " ")
        if definition:
            hook_source = hook_source.replace(definition, " ")
    else:
        hook_source = first
    hook = _opening_hook_for(fmt, hook_source)
    prayer_pos = first.find(PRAYER_SENTENCE)
    if not hook or prayer_pos < len(hook):
        raise RuntimeError("identity sequence order must begin hook -> prayer")
    if fmt == "short" and not " ".join(first[:prayer_pos].split()).strip() == hook:
        raise RuntimeError("identity sequence: the Short prayer must follow the first two sentences")
    if fmt != "podcast" and definition:
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
