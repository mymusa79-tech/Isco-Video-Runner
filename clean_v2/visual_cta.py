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
_COMBO = _ASSET_DIR / "subscribe_bell_reference.mp4"
SFX_TARGET_REL_DB = -12.0
SFX_MIN_REL_DB = -16.0
SFX_MAX_REL_DB = -9.0
SHORT_CTA_CENTER_X = 540
SHORT_CTA_Y = 1080
HORIZONTAL_CTA_CENTER_X = 960
HORIZONTAL_CTA_Y = 500
HORIZONTAL_KEY_TEXT_Y = 770
_ICON_BY_MODE = {
    "like": _ASSET_DIR / "like_ORIGINAL.png",
    "comment": _ASSET_DIR / "comment_ORIGINAL.png",
    "share": _ASSET_DIR / "share_ORIGINAL.png",
    "bell": _ASSET_DIR / "bell_ORIGINAL.png",
}

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


def _events(
    *,
    fmt: str,
    duration: float,
    script: Mapping[str, Any],
    authored_mode: str,
) -> list[VisualCtaEvent]:
    if fmt == "short":
        # Approved Short rule: never more than two overlays. Keep both away from
        # the opening hook and the final payoff/outro seam.
        count = 2 if duration >= 28.0 else 1
        starts = [max(7.0, duration * 0.48)]
        if count == 2:
            starts.append(min(duration - 4.0, duration * 0.74))
        modes = [_short_first_mode(script), "subscribe_combo"]
        return [
            VisualCtaEvent(
                mode=modes[index],
                start_seconds=round(start, 3),
                end_seconds=round(min(duration - 1.2, start + (2.6 if modes[index] == "subscribe_combo" else 1.35)), 3),
                x=(160 if modes[index] == "subscribe_combo" else 465),
                y=SHORT_CTA_Y,
                asset="subscribe_bell_reference.mp4" if modes[index] == "subscribe_combo" else _ICON_BY_MODE[modes[index]].name,
            )
            for index, start in enumerate(starts)
            if start < duration - 2.0
        ][:2]

    if fmt not in {"film", "podcast"}:
        return []

    # Horizontal long-form shares one sparse CTA policy. Podcast stays calmer
    # than Film because narration and key text carry more of the experience.
    if fmt == "podcast":
        points = [0.56, 0.82] if duration >= 120 else [0.68]
    elif duration < 180:
        points = [0.48, 0.80]
    elif duration < 420:
        points = [0.28, 0.56, 0.82]
    else:
        points = [0.22, 0.44, 0.65, 0.84]

    primary = authored_mode if authored_mode in {"like", "comment", "share"} else "comment"
    palette = ["like", primary, "share", "subscribe_combo"]
    if len(points) == 1:
        palette = ["subscribe_combo"]
    elif len(points) == 2:
        palette = [primary, "subscribe_combo"]
    elif len(points) == 3:
        palette = ["like" if primary != "like" else "comment", primary, "subscribe_combo"]

    events: list[VisualCtaEvent] = []
    last_mode = ""
    for index, ratio in enumerate(points):
        mode = palette[min(index, len(palette) - 1)]
        if mode == last_mode and mode != "subscribe_combo":
            mode = "like" if mode != "like" else "comment"
        start = max(12.0, duration * ratio)
        if start > duration - 15.0:
            continue
        events.append(
            VisualCtaEvent(
                mode=mode,
                start_seconds=round(start, 3),
                end_seconds=round(start + (3.5 if mode == "subscribe_combo" else 1.45), 3),
                x=(610 if mode == "subscribe_combo" else 908),
                y=HORIZONTAL_CTA_Y,
                asset="subscribe_bell_reference.mp4" if mode == "subscribe_combo" else _ICON_BY_MODE[mode].name,
            )
        )
        last_mode = mode
    return events


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
    input_specs: list[tuple[str, int, VisualCtaEvent]] = []
    next_index = 1
    for event in events:
        if event.mode == "subscribe_combo":
            command.extend(["-i", str(_COMBO)])
            input_specs.append(("combo", next_index, event))
        else:
            command.extend(["-loop", "1", "-framerate", "30", "-i", str(_ICON_BY_MODE[event.mode])])
            input_specs.append(("icon", next_index, event))
        next_index += 1

    # one copy of the approved click for every single-icon event; combo keeps
    # its own original click/audio from the user's reference video.
    click_specs: list[tuple[int, VisualCtaEvent]] = []
    for event in events:
        if event.mode != "subscribe_combo":
            command.extend(["-i", str(_CLICK)])
            click_specs.append((next_index, event))
            next_index += 1

    narration_mean_db = _mean_db(Path(narration_path))
    click_gain_db = _sfx_gain_db(source=_CLICK, narration_mean_db=narration_mean_db)
    combo_gain_db = _sfx_gain_db(source=_COMBO, narration_mean_db=narration_mean_db)

    filters: list[str] = []
    current = "[0:v]"
    audio_labels = ["[0:a]"]

    for number, (kind, input_index, event) in enumerate(input_specs):
        label = f"cta{number}"
        out = f"vcta{number}"
        if kind == "icon":
            duration = max(0.5, event.end_seconds - event.start_seconds)
            fade_out = max(0.2, duration - 0.18)
            filters.append(
                f"[{input_index}:v]scale={icon_size}:{icon_size},format=rgba,"
                f"fade=t=in:st=0:d=0.12:alpha=1,"
                f"fade=t=out:st={fade_out:.3f}:d=0.18:alpha=1,"
                f"trim=duration={duration:.3f},setpts=PTS-STARTPTS+{event.start_seconds:.3f}/TB[{label}]"
            )
        else:
            combo_width = 760 if fmt == "short" else 700
            combo_duration = max(0.8, event.end_seconds - event.start_seconds)
            palette_filter = (
                "hue=h=38:s=0.72,eq=contrast=1.04:brightness=-0.01"
                if fmt == "short"
                else "null"
            )
            filters.append(
                f"[{input_index}:v]trim=start=0.45:duration={combo_duration:.3f},setpts=PTS-STARTPTS,"
                "crop=1020:360:130:170,format=rgba,colorkey=0xFFFFFF:0.16:0.08,"
                f"{palette_filter},scale={combo_width}:-1,"
                f"setpts=PTS+{event.start_seconds:.3f}/TB[{label}]"
            )
            delay = int(round(event.start_seconds * 1000))
            filters.append(
                f"[{input_index}:a]atrim=start=0.45:duration={combo_duration:.3f},asetpts=PTS-STARTPTS,"
                f"adelay={delay}|{delay},volume={combo_gain_db:.3f}dB[acombo{number}]"
            )
            audio_labels.append(f"[acombo{number}]")

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
    _run(command)


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

    required = [_CLICK, _COMBO, *_ICON_BY_MODE.values()]
    for asset in required:
        if not asset.is_file() or asset.stat().st_size <= 1024:
            raise RuntimeError(f"approved CTA asset missing: {asset.name}")

    total = float(probe_duration(Path(narration_path)))
    authored = _authored_mode(output_dir)
    events = _events(fmt=fmt, duration=total, script=script, authored_mode=authored)

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
        "short_max_two": fmt != "short" or len(events) <= 2,
        "short_combo_max_seconds": 2.6 if fmt == "short" else None,
        "short_combo_palette": "warm_gold_dark_harmonized" if fmt == "short" else None,
        "one_action_per_normal_event": True,
        "combo_is_single_approved_reference_asset": True,
        "safe_zone_policy": "left_or_side_midfield_away_from_youtube_right_rail_and_bottom_ui",
        "click_asset": _CLICK.name,
        "click_mix_policy": "measured_below_voice_above_background_music",
        "sfx_target_relative_db": SFX_TARGET_REL_DB,
        "sfx_allowed_relative_db": [SFX_MIN_REL_DB, SFX_MAX_REL_DB],
        "short_cta_position": (
            {"center_x": SHORT_CTA_CENTER_X, "y": SHORT_CTA_Y, "caption_y": 1400}
            if fmt == "short"
            else None
        ),
        "horizontal_cta_position": (
            {
                "center_x": HORIZONTAL_CTA_CENTER_X,
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
