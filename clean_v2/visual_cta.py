from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .media import probe_duration

_ASSET_DIR = Path(__file__).resolve().parent / "assets" / "identity"
_CLICK = _ASSET_DIR / "click_ORIGINAL.wav"
SFX_TARGET_REL_DB = -12.0
SFX_MIN_REL_DB = -16.0
SFX_MAX_REL_DB = -9.0
SHORT_CTA_CENTER_X = 180
SHORT_CTA_CENTER_Y = 910
SHORT_CTA_X = 72
SHORT_CTA_Y = 790
SHORT_CTA_ICON_SIZE = 170
SHORT_CTA_CARD_WIDTH = 220
SHORT_CTA_CARD_HEIGHT = 250
HORIZONTAL_CTA_CENTER_X = 320
HORIZONTAL_CTA_CENTER_Y = 540
HORIZONTAL_CTA_Y = 488
HORIZONTAL_KEY_TEXT_Y = 770
SHORT_COMBO_WIDTH = 520
HORIZONTAL_COMBO_WIDTH = 560
_ICON_BY_MODE = {
    "like": _ASSET_DIR / "like_ORIGINAL.png",
    "comment": _ASSET_DIR / "comment_ORIGINAL.png",
    "share": _ASSET_DIR / "share_ORIGINAL.png",
    "bell": _ASSET_DIR / "bell_ORIGINAL.png",
}
_SHORT_LABEL_BY_MODE = {
    "like": "إعجاب",
    "comment": "تعليق",
    "share": "مشاركة",
    "bell": "تنبيهات",
}

_CTA_SEMANTIC_FAMILIES = {
    "comment": (
        "اكتب", "كتابة", "يكتب", "تكتب", "قلم", "دفتر", "ملاحظة", "ملاحظات",
        "رسالة", "تعليق", "write", "writing", "written", "pen", "notebook",
        "journal", "note", "notes", "typing", "keyboard", "message", "comment",
    ),
    "share": (
        "شارك", "مشاركة", "أرسل", "ارسل", "نشر", "share", "sharing", "send",
        "forward", "pass along",
    ),
    "like": (
        "إعجاب", "اعجاب", "أعجب", "اعجب", "قلب", "يحب", "like", "liked",
        "heart", "approve", "approval",
    ),
    "subscribe_combo": (
        "اشترك", "اشتراك", "تابع", "متابعة", "انضم", "subscribe", "subscription",
        "follow", "join",
    ),
}

_CTA_FALLBACK_ORDER = ("like", "share", "comment")

_SECRET_NAMES = {
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "MISTRAL_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "CLOUDFLARE_API_TOKEN",
    "AZURE_SPEECH_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}


@dataclass(frozen=True)
class VisualCtaEvent:
    mode: str
    start_seconds: float
    end_seconds: float
    x: int
    y: int
    asset: str


def _env() -> dict[str, str]:
    value = os.environ.copy()
    for name in list(value):
        upper = name.upper()
        if name in _SECRET_NAMES or upper.endswith("_API_KEY") or upper.endswith("_TOKEN"):
            value.pop(name, None)
    return value


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True, env=_env(), timeout=240)


def _cairo_bold_font_path() -> Path:
    local = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")) / "fonts" / "isco-cairo" / "Cairo.ttf"
    if local.is_file():
        return local
    proc = subprocess.run(
        ["fc-match", "-f", "%{file}\n", "Cairo:weight=bold"],
        check=False, capture_output=True, text=True, env=_env(), timeout=30,
    )
    candidate = Path((proc.stdout or "").splitlines()[0].strip()) if (proc.stdout or "").strip() else None
    if candidate and candidate.is_file():
        return candidate
    raise RuntimeError("cairo_bold_font_missing_for_cta")


def _render_arabic_subscribe_combo(destination: Path, *, fmt: str) -> Path:
    """Create one local Arabic Subscribe + Bell CTA. No provider call, no English UI."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("pillow_missing_for_cta") from exc

    width = SHORT_COMBO_WIDTH if fmt == "short" else HORIZONTAL_COMBO_WIDTH
    height = 150 if fmt == "short" else 142
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    bell_size = height - 24
    gap = 14
    pill_width = width - bell_size - gap
    pill_box = (0, 12, pill_width, height - 12)
    bell_box = (pill_width + gap, 12, width, height - 12)

    draw.rounded_rectangle(
        pill_box,
        radius=(height - 24) // 2,
        fill=(214, 42, 51, 248),
        outline=(244, 240, 233, 245),
        width=3,
    )
    draw.ellipse(
        bell_box,
        fill=(246, 243, 237, 248),
        outline=(24, 22, 20, 90),
        width=2,
    )

    font_size = 62 if fmt == "short" else 58
    font = ImageFont.truetype(
        str(_cairo_bold_font_path()),
        font_size,
        layout_engine=ImageFont.Layout.RAQM,
    )
    text_x = pill_width // 2
    text_y = height // 2 - 2
    draw.text(
        (text_x, text_y),
        "اشترك",
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=(246, 243, 237, 255),
        stroke_width=1,
        stroke_fill=(110, 18, 25, 190),
    )

    bell = Image.open(_ICON_BY_MODE["bell"]).convert("RGBA")
    target = bell_size - 42
    bell.thumbnail((target, target), Image.Resampling.LANCZOS)
    bx = bell_box[0] + (bell_size - bell.width) // 2
    by = 12 + (bell_size - bell.height) // 2
    canvas.alpha_composite(bell, (bx, by))

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, "PNG")
    return destination


def _render_short_labeled_icon(destination: Path, *, mode: str) -> Path:
    """Render one visual-only Short CTA icon with its Arabic action label below it."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("pillow_missing_for_cta") from exc

    if mode not in _SHORT_LABEL_BY_MODE or mode not in _ICON_BY_MODE:
        raise ValueError(f"unsupported short CTA mode: {mode}")

    canvas = Image.new(
        "RGBA",
        (SHORT_CTA_CARD_WIDTH, SHORT_CTA_CARD_HEIGHT),
        (0, 0, 0, 0),
    )
    icon = Image.open(_ICON_BY_MODE[mode]).convert("RGBA")
    icon.thumbnail((SHORT_CTA_ICON_SIZE, SHORT_CTA_ICON_SIZE), Image.Resampling.LANCZOS)
    ix = (SHORT_CTA_CARD_WIDTH - icon.width) // 2
    iy = 8
    canvas.alpha_composite(icon, (ix, iy))

    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(
        str(_cairo_bold_font_path()),
        42,
        layout_engine=ImageFont.Layout.RAQM,
    )
    draw.text(
        (SHORT_CTA_CARD_WIDTH // 2, SHORT_CTA_ICON_SIZE + 52),
        _SHORT_LABEL_BY_MODE[mode],
        font=font,
        anchor="mm",
        direction="rtl",
        language="ar",
        fill=(246, 243, 237, 255),
        stroke_width=3,
        stroke_fill=(18, 22, 28, 220),
    )

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(destination, "PNG")
    return destination


def _mean_db(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-af", "volumedetect", "-f", "null", "-",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=240,
    )
    match = __import__("re").search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr or "")
    if not match:
        raise RuntimeError(f"CTA SFX level measurement missing: {path.name}")
    return float(match.group(1))


def _sfx_gain_db(*, source: Path, narration_mean_db: float) -> float:
    source_mean = _mean_db(source)
    return (narration_mean_db + SFX_TARGET_REL_DB) - source_mean


def _authored_mode(output_dir: Path) -> str:
    path = Path(output_dir) / "cta-plan.json"
    if not path.is_file():
        return "none"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "none"
    mode = str(raw.get("mode") or "none").strip().lower()
    return mode if mode in {"like", "comment", "share", "subscribe"} else "none"


def _short_first_mode(script: Mapping[str, Any]) -> str:
    title = str(script.get("title") or "")
    if any(token in title for token in ("؟", "لماذا", "كيف", "ماذا", "هل ")):
        return "comment"
    return "like"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _semantic_context_at(
    *,
    output_dir: Path,
    script: Mapping[str, Any],
    start_seconds: float,
) -> str:
    timeline = _read_json(Path(output_dir) / "timeline-first.json")
    section_id = ""
    for item in timeline.get("section_events") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            start = float(item.get("start"))
            end = float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if start <= start_seconds < end:
            section_id = str(item.get("section_id") or "")
            break

    fragments: list[str] = []
    for section in script.get("sections") or []:
        if isinstance(section, Mapping) and str(section.get("id") or "") == section_id:
            fragments.append(str(section.get("narration") or ""))
            break

    story = _read_json(Path(output_dir) / "visual-story.json")
    for beat in story.get("beats") or []:
        if not isinstance(beat, Mapping) or str(beat.get("section_id") or "") != section_id:
            continue
        for key in ("viewer_intent", "meaning_target", "shot_intent"):
            fragments.append(str(beat.get(key) or ""))
        for key in ("semantic_must_have", "semantic_should_avoid"):
            value = beat.get(key)
            if isinstance(value, list):
                fragments.extend(str(item) for item in value)
            elif value:
                fragments.append(str(value))
    return " ".join(" ".join(fragments).lower().split())


def _cta_conflicts_with_context(mode: str, context: str) -> bool:
    normalized = " ".join(str(context or "").lower().split())
    return any(token in normalized for token in _CTA_SEMANTIC_FAMILIES.get(mode, ()))


def _cta_position(*, mode: str, fmt: str) -> tuple[int, int]:
    """Keep CTA in a safe left-side field, away from captions and key text."""
    if fmt == "short":
        if mode == "subscribe_combo":
            return (
                max(40, min(1080 - SHORT_COMBO_WIDTH - 20, SHORT_CTA_CENTER_X - SHORT_COMBO_WIDTH // 2)),
                SHORT_CTA_CENTER_Y - 88,
            )
        return (SHORT_CTA_X, SHORT_CTA_Y)
    if mode == "subscribe_combo":
        return (
            max(40, min(1920 - HORIZONTAL_COMBO_WIDTH - 40, HORIZONTAL_CTA_CENTER_X - HORIZONTAL_COMBO_WIDTH // 2)),
            HORIZONTAL_CTA_CENTER_Y - 99,
        )
    return (HORIZONTAL_CTA_CENTER_X - 52, HORIZONTAL_CTA_Y)


def _event_with_mode(event: VisualCtaEvent, *, mode: str, fmt: str) -> VisualCtaEvent:
    x, y = _cta_position(mode=mode, fmt=fmt)
    return VisualCtaEvent(
        mode=mode,
        start_seconds=event.start_seconds,
        end_seconds=event.end_seconds,
        x=x,
        y=y,
        asset="arabic_subscribe_combo_renderer" if mode == "subscribe_combo" else _ICON_BY_MODE[mode].name,
    )


def _enforce_semantic_separation(
    *,
    events: list[VisualCtaEvent],
    output_dir: Path,
    script: Mapping[str, Any],
    fmt: str,
) -> tuple[list[VisualCtaEvent], list[dict[str, Any]]]:
    revised: list[VisualCtaEvent] = []
    decisions: list[dict[str, Any]] = []
    used: set[str] = set()
    for event in events:
        context = _semantic_context_at(
            output_dir=output_dir,
            script=script,
            start_seconds=event.start_seconds,
        )
        preferred = event.mode
        candidates = [preferred, *_CTA_FALLBACK_ORDER]
        chosen = preferred
        for candidate in dict.fromkeys(candidates):
            if candidate in used and candidate != "subscribe_combo":
                continue
            if not _cta_conflicts_with_context(candidate, context):
                chosen = candidate
                break
        revised_event = _event_with_mode(event, mode=chosen, fmt=fmt)
        revised.append(revised_event)
        used.add(chosen)
        decisions.append(
            {
                "start_seconds": event.start_seconds,
                "preferred_mode": preferred,
                "selected_mode": chosen,
                "semantic_conflict_avoided": chosen != preferred,
            }
        )
    return revised, decisions


def _events(
    *,
    fmt: str,
    duration: float,
    script: Mapping[str, Any],
    authored_mode: str,
) -> list[VisualCtaEvent]:
    if fmt == "short":
        # Short CTA is intentionally visual-only: one quiet social cue, never spoken.
        start = max(7.0, duration * 0.56)
        mode = _short_first_mode(script)
        if start >= duration - 3.0:
            return []
        x, y = _cta_position(mode=mode, fmt=fmt)
        return [
            VisualCtaEvent(
                mode=mode,
                start_seconds=round(start, 3),
                end_seconds=round(min(duration - 2.0, start + 1.35), 3),
                x=x,
                y=y,
                asset=_ICON_BY_MODE[mode].name,
            )
        ]

    if fmt not in {"film", "podcast"}:
        return []

    # Fallback-only long-form behavior: one authored action, never a rotating
    # palette of unrelated like/share/comment prompts.
    mode = str(authored_mode or "none")
    if mode not in {"like", "comment", "share", "subscribe"}:
        return []
    render_mode = "subscribe_combo" if mode == "subscribe" else mode
    ratio = 0.66 if fmt == "podcast" else 0.60
    start = max(30.5, duration * ratio)
    if start >= duration - 12.0:
        return []
    x, y = _cta_position(mode=render_mode, fmt=fmt)
    event_seconds = 3.0 if render_mode == "subscribe_combo" else 1.8
    return [
        VisualCtaEvent(
            mode=render_mode,
            start_seconds=round(start, 3),
            end_seconds=round(min(duration - 10.0, start + event_seconds), 3),
            x=x,
            y=y,
            asset=(
                "arabic_subscribe_combo_renderer"
                if render_mode == "subscribe_combo"
                else _ICON_BY_MODE[render_mode].name
            ),
        )
    ]


def _longform_contextual_event(
    *,
    output_dir: Path,
    fmt: str,
    duration: float,
) -> list[VisualCtaEvent]:
    """Render exactly the same CTA action scheduled for the spoken long-form line."""
    if fmt not in {"film", "podcast"}:
        return []
    raw = _read_json(Path(output_dir) / "cta-plan.json")
    if raw.get("visual_only") is True or not str(raw.get("spoken_text") or "").strip():
        return []
    mode = str(raw.get("mode") or "none").strip().lower()
    if mode not in {"like", "comment", "share", "subscribe"}:
        return []
    schedule = raw.get("schedule")
    if not isinstance(schedule, Mapping):
        return []
    try:
        start = float(schedule.get("start_seconds"))
        scheduled_end = float(schedule.get("end_seconds"))
    except (TypeError, ValueError):
        return []
    if start < 0 or scheduled_end <= start or start >= duration:
        return []

    render_mode = "subscribe_combo" if mode == "subscribe" else mode
    x, y = _cta_position(mode=render_mode, fmt=fmt)
    display_seconds = 3.0 if render_mode == "subscribe_combo" else 1.8
    end = min(duration, scheduled_end, start + display_seconds)
    if end - start < 0.6:
        return []
    return [
        VisualCtaEvent(
            mode=render_mode,
            start_seconds=round(start, 3),
            end_seconds=round(end, 3),
            x=x,
            y=y,
            asset=(
                "arabic_subscribe_combo_renderer"
                if render_mode == "subscribe_combo"
                else _ICON_BY_MODE[render_mode].name
            ),
        )
    ]

def _render(
    *,
    video: Path,
    dest: Path,
    events: list[VisualCtaEvent],
    fmt: str,
    narration_path: Path,
) -> None:
    if not events:
        return

    width, height = ((1080, 1920) if fmt == "short" else (1920, 1080))
    icon_size = 150 if fmt == "short" else 105

    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video),
    ]
    combo_path = dest.parent / ".arabic-subscribe-combo.png"
    if any(event.mode == "subscribe_combo" for event in events):
        _render_arabic_subscribe_combo(combo_path, fmt=fmt)

    short_labeled_paths: dict[str, Path] = {}
    if fmt == "short":
        for event in events:
            if event.mode == "subscribe_combo":
                continue
            labeled = dest.parent / f".short-cta-{event.mode}.png"
            _render_short_labeled_icon(labeled, mode=event.mode)
            short_labeled_paths[event.mode] = labeled

    input_specs: list[tuple[str, int, VisualCtaEvent]] = []
    next_index = 1
    for event in events:
        if event.mode == "subscribe_combo":
            command.extend(["-loop", "1", "-framerate", "30", "-i", str(combo_path)])
            input_specs.append(("combo", next_index, event))
        else:
            asset = short_labeled_paths.get(event.mode, _ICON_BY_MODE[event.mode])
            command.extend(["-loop", "1", "-framerate", "30", "-i", str(asset)])
            input_specs.append(("icon", next_index, event))
        next_index += 1

    # One measured click per CTA event. The combined Arabic CTA is renderer-owned
    # too, so it no longer inherits English reference-video audio or UI.
    click_specs: list[tuple[int, VisualCtaEvent]] = []
    for event in events:
        command.extend(["-i", str(_CLICK)])
        click_specs.append((next_index, event))
        next_index += 1

    narration_mean_db = _mean_db(Path(narration_path))
    click_gain_db = _sfx_gain_db(source=_CLICK, narration_mean_db=narration_mean_db)

    filters: list[str] = []
    current = "[0:v]"
    audio_labels = ["[0:a]"]

    for number, (kind, input_index, event) in enumerate(input_specs):
        label = f"cta{number}"
        out = f"vcta{number}"
        if kind == "icon":
            duration = max(0.5, event.end_seconds - event.start_seconds)
            fade_out = max(0.2, duration - 0.18)
            scale_filter = (
                f"scale={SHORT_CTA_CARD_WIDTH}:{SHORT_CTA_CARD_HEIGHT},"
                if fmt == "short"
                else f"scale={icon_size}:{icon_size},"
            )
            filters.append(
                f"[{input_index}:v]{scale_filter}format=rgba,"
                f"fade=t=in:st=0:d=0.12:alpha=1,"
                f"fade=t=out:st={fade_out:.3f}:d=0.18:alpha=1,"
                f"trim=duration={duration:.3f},setpts=PTS-STARTPTS+{event.start_seconds:.3f}/TB[{label}]"
            )
        else:
            combo_duration = min(2.6 if fmt == "short" else 3.0, max(0.8, event.end_seconds - event.start_seconds))
            fade_out = max(0.25, combo_duration - 0.20)
            filters.append(
                f"[{input_index}:v]format=rgba,"
                f"fade=t=in:st=0:d=0.14:alpha=1,"
                f"fade=t=out:st={fade_out:.3f}:d=0.20:alpha=1,"
                f"trim=duration={combo_duration:.3f},"
                f"setpts=PTS-STARTPTS+{event.start_seconds:.3f}/TB[{label}]"
            )

        filters.append(
            f"{current}[{label}]overlay=x={event.x}:y={event.y}:eof_action=pass:format=auto[{out}]"
        )
        current = f"[{out}]"

    for number, (input_index, event) in enumerate(click_specs):
        delay = int(round((event.start_seconds + 0.55) * 1000))
        filters.append(
            f"[{input_index}:a]adelay={delay}|{delay},volume={click_gain_db:.3f}dB[aclick{number}]"
        )
        audio_labels.append(f"[aclick{number}]")

    filters.append(
        "".join(audio_labels)
        + f"amix=inputs={len(audio_labels)}:normalize=0:duration=first:dropout_transition=0,"
        "alimiter=limit=0.95:level=disabled[aout]"
    )

    command.extend([
        "-filter_complex", ";".join(filters),
        "-map", current,
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        "-t", f"{probe_duration(video):.3f}",
        str(dest),
    ])
    try:
        _run(command)
    finally:
        combo_path.unlink(missing_ok=True)
        for path in short_labeled_paths.values():
            path.unlink(missing_ok=True)


def apply_visual_cta_assets(
    *,
    output_dir: Path,
    final_path: Path,
    narration_path: Path,
    script: Mapping[str, Any],
    fmt: str,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    final_path = Path(final_path)

    if fmt not in {"short", "film", "podcast"}:
        return {
            "schema_version": 1,
            "source": "clean-v2-approved-visual-cta-v1",
            "status": "not_applicable",
            "format": fmt,
            "provider_calls_added": 0,
        }

    required = [_CLICK, *_ICON_BY_MODE.values()]
    for asset in required:
        if not asset.is_file() or asset.stat().st_size <= 1024:
            raise RuntimeError(f"approved CTA asset missing: {asset.name}")

    total = float(probe_duration(Path(narration_path)))
    authored = _authored_mode(output_dir)
    if fmt in {"film", "podcast"}:
        events = _longform_contextual_event(
            output_dir=output_dir,
            fmt=fmt,
            duration=total,
        )
        semantic_decisions = [
            {
                "start_seconds": event.start_seconds,
                "preferred_mode": authored,
                "selected_mode": event.mode,
                "semantic_conflict_avoided": False,
                "spoken_cta_aligned": True,
            }
            for event in events
        ]
    else:
        events = _events(
            fmt=fmt,
            duration=total,
            script=script,
            authored_mode=authored,
        )
        events, semantic_decisions = _enforce_semantic_separation(
            events=events,
            output_dir=output_dir,
            script=script,
            fmt=fmt,
        )

    temp = output_dir / ".approved-visual-cta.mp4"
    temp.unlink(missing_ok=True)
    try:
        if events:
            _render(video=final_path, dest=temp, events=events, fmt=fmt, narration_path=Path(narration_path))
            os.replace(temp, final_path)
            status = "applied"
        else:
            status = "not_scheduled"
    finally:
        temp.unlink(missing_ok=True)

    report = {
        "schema_version": 1,
        "source": "clean-v2-approved-visual-cta-v1",
        "status": status,
        "format": fmt,
        "events": [asdict(item) for item in events],
        "event_count": len(events),
        "short_max_two": fmt != "short" or len(events) <= 1,
        "short_combo_max_seconds": 2.6 if fmt == "short" else None,
        "short_combo_palette": "red_offwhite_arabic_renderer_owned" if fmt == "short" else None,
        "one_action_per_normal_event": True,
        "combo_is_single_approved_reference_asset": False,
        "combo_renderer": "local_pillow_cairo_bold_arabic_subscribe_plus_bell",
        "semantic_separation": fmt == "short",
        "spoken_visual_alignment": fmt in {"film", "podcast"},
        "longform_cta_policy": (
            "one_spoken_contextual_cta_with_matching_visual"
            if fmt in {"film", "podcast"}
            else None
        ),
        "subscribe_delivery": (
            "matching_spoken_contextual_cta"
            if fmt in {"film", "podcast"}
            else "existing_short_visual_policy"
        ),
        "semantic_separation_policy": (
            "longform visual CTA must match the spoken CTA exactly; short keeps visual-only separation"
        ),
        "semantic_decisions": semantic_decisions,
        "safe_zone_policy": "right_midfield_clear_of_caption_and_bottom_ui",
        "click_asset": _CLICK.name,
        "click_mix_policy": "measured_below_voice_above_background_music",
        "sfx_target_relative_db": SFX_TARGET_REL_DB,
        "sfx_allowed_relative_db": [SFX_MIN_REL_DB, SFX_MAX_REL_DB],
        "short_cta_position": (
            {"center_x": SHORT_CTA_CENTER_X, "center_y": SHORT_CTA_CENTER_Y, "y": SHORT_CTA_Y, "caption_y": 1400}
            if fmt == "short"
            else None
        ),
        "horizontal_cta_position": (
            {
                "center_x": HORIZONTAL_CTA_CENTER_X,
                "center_y": HORIZONTAL_CTA_CENTER_Y,
                "y": HORIZONTAL_CTA_Y,
                "podcast_key_text_y": HORIZONTAL_KEY_TEXT_Y if fmt == "podcast" else None,
            }
            if fmt in {"film", "podcast"}
            else None
        ),
        "provider_calls_added": 0,
    }
    (output_dir / "visual-cta.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report
