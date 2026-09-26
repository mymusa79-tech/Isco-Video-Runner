from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .media import probe_duration

CONTRACT_ID = "clean-v2-timeline-first-v1"
DEFAULT_SHORT_SAFETY_MAX_SECONDS = 120.0
DEFAULT_FILM_SAFETY_MAX_SECONDS = 3600.0
FINAL_DURATION_TOLERANCE_SECONDS = 0.08


class TimelineFirstError(RuntimeError):
    """Fail-closed operational timeline violation."""

    def __init__(self, message: str, *, report: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = dict(report or {})


def _positive(value: object, field: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise TimelineFirstError(f"TIMELINE_FIRST_INVALID_{field.upper()}") from None
    if seconds <= 0:
        raise TimelineFirstError(f"TIMELINE_FIRST_INVALID_{field.upper()}")
    return seconds


def safety_max_seconds(fmt: str) -> float:
    if fmt == "short":
        default = DEFAULT_SHORT_SAFETY_MAX_SECONDS
        specific = "CLEAN_V2_SHORT_SAFETY_MAX_SECONDS"
    elif fmt == "film":
        default = DEFAULT_FILM_SAFETY_MAX_SECONDS
        specific = "CLEAN_V2_FILM_SAFETY_MAX_SECONDS"
    else:
        default = DEFAULT_FILM_SAFETY_MAX_SECONDS
        specific = "CLEAN_V2_DURATION_SAFETY_MAX_SECONDS"
    raw = str(
        os.environ.get(specific)
        or os.environ.get("CLEAN_V2_DURATION_SAFETY_MAX_SECONDS")
        or default
    ).strip()
    maximum = _positive(raw, "safety_max_seconds")
    return maximum


def retime_events(
    events: Sequence[Mapping[str, Any]],
    *,
    source_seconds: float,
    target_seconds: float,
) -> list[dict[str, Any]]:
    """Scale measured audio windows onto the mastered narration without rewriting time."""
    source = _positive(source_seconds, "source_seconds")
    target = _positive(target_seconds, "target_seconds")
    if not events:
        raise TimelineFirstError("TIMELINE_FIRST_EVENTS_MISSING")

    scale = target / source
    result: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, raw in enumerate(events):
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") or 0.0)
        if end <= start:
            raise TimelineFirstError("TIMELINE_FIRST_EVENT_INVALID")
        mapped_start = max(previous_end, start * scale)
        mapped_end = target if index == len(events) - 1 else min(target, end * scale)
        mapped_end = max(mapped_start, mapped_end)
        item = dict(raw)
        item.update(
            {
                "start": round(mapped_start, 3),
                "end": round(mapped_end, 3),
                "duration_seconds": round(mapped_end - mapped_start, 3),
            }
        )
        result.append(item)
        previous_end = mapped_end
    return result


def _load_voice_units(output_dir: Path) -> list[dict[str, Any]]:
    report_path = output_dir / "voice-sections.json"
    if not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    sections = report.get("sections") if isinstance(report, Mapping) else None
    if not isinstance(sections, list):
        return []

    units: list[dict[str, Any]] = []
    cursor = 0.0
    for section_index, section in enumerate(sections, start=1):
        if not isinstance(section, Mapping):
            continue
        section_id = str(section.get("id") or f"s{section_index}").strip()
        chunks = section.get("chunks")
        if not isinstance(chunks, list) or not chunks:
            continue
        for chunk_index, chunk in enumerate(chunks, start=1):
            if not isinstance(chunk, Mapping):
                continue
            relative = str(chunk.get("file") or "").strip()
            path = output_dir / relative
            if not relative or not path.is_file():
                raise TimelineFirstError(
                    f"TIMELINE_FIRST_AUDIO_UNIT_MISSING section={section_id} chunk={chunk_index}"
                )
            seconds = _positive(probe_duration(path), "audio_unit_seconds")
            role = str(chunk.get("role") or "topic").strip()
            units.append(
                {
                    "section_id": section_id,
                    "chunk": chunk_index,
                    "role": role,
                    "provider": str(chunk.get("provider") or ""),
                    "file": relative,
                    "start": cursor,
                    "end": cursor + seconds,
                }
            )
            cursor += seconds
    return units


def _fallback_section_units(output_dir: Path) -> list[dict[str, Any]]:
    audio_dir = output_dir / "audio"
    if not audio_dir.is_dir():
        return []
    paths = sorted(
        path
        for path in audio_dir.glob("*.wav")
        if path.is_file() and path.name[:2].isdigit()
    )
    units: list[dict[str, Any]] = []
    cursor = 0.0
    for index, path in enumerate(paths, start=1):
        seconds = _positive(probe_duration(path), "section_audio_seconds")
        units.append(
            {
                "section_id": f"s{index}",
                "chunk": 1,
                "role": "hook" if index == 1 else ("outro" if index == len(paths) else "topic"),
                "file": str(path.relative_to(output_dir)),
                "start": cursor,
                "end": cursor + seconds,
            }
        )
        cursor += seconds
    return units


def _section_events(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for unit in units:
        section_id = str(unit.get("section_id") or "").strip()
        if not section_id:
            raise TimelineFirstError("TIMELINE_FIRST_SECTION_ID_MISSING")
        if result and result[-1]["section_id"] == section_id:
            result[-1]["end"] = float(unit["end"])
            continue
        result.append(
            {
                "section_id": section_id,
                "start": float(unit["start"]),
                "end": float(unit["end"]),
            }
        )
    return result


def _one_role(units: Sequence[Mapping[str, Any]], role: str) -> Mapping[str, Any] | None:
    matches = [item for item in units if str(item.get("role") or "") == role]
    if not matches:
        return None
    if len(matches) != 1:
        raise TimelineFirstError(f"TIMELINE_FIRST_ROLE_COUNT role={role} count={len(matches)}")
    return matches[0]


def _identity_events(
    units: Sequence[Mapping[str, Any]],
    *,
    voice_seconds: float,
    require_identity: bool,
    fmt: str = "short",
) -> list[dict[str, Any]]:
    hook = _one_role(units, "hook")
    prayer = _one_role(units, "prayer")
    identity = _one_role(units, "channel_identity")
    outro = _one_role(units, "outro")
    intro_silence = _one_role(units, "intro_silence")
    final_silence = _one_role(units, "final_silence")

    identity_missing_silence = fmt in {"short", "film", "podcast"} and (
        intro_silence is None or final_silence is None
    )
    if require_identity and (
        hook is None
        or prayer is None
        or identity is None
        or outro is None
        or identity_missing_silence
    ):
        raise TimelineFirstError(
            "TIMELINE_FIRST_IDENTITY_AUDIO_BOUNDS_MISSING "
            f"hook={hook is not None} prayer={prayer is not None} "
            f"identity={identity is not None} outro={outro is not None} "
            f"intro_silence={intro_silence is not None} final_silence={final_silence is not None}"
        )
    if prayer is None or identity is None:
        return []

    intro_start = float(intro_silence["start"])
    intro_end = float(intro_silence["end"])
    topic_start = float(identity["end"])
    topic_end = float(outro["start"]) if outro is not None else voice_seconds
    events: list[dict[str, Any]] = []
    if hook is not None:
        events.append(
            {
                "kind": "hook",
                "source": "measured_voice_chunk",
                "start": float(hook["start"]),
                "end": float(hook["end"]),
            }
        )
    events.extend(
        [
            {
                "kind": "intro",
                "source": (
                    "measured_native_nabra_pause"
                    if str(intro_silence.get("provider") or "") == "nabra_native_pause"
                    else "measured_intro_silence"
                ),
                "start": intro_start,
                "end": intro_end,
            },
            {
                "kind": "prayer",
                "source": "measured_voice_chunk",
                "start": float(prayer["start"]),
                "end": float(prayer["end"]),
            },
            {
                "kind": "channel_identity",
                "source": "measured_voice_chunk",
                "start": float(identity["start"]),
                "end": float(identity["end"]),
            },
        ]
    )
    if topic_end > topic_start:
        events.append(
            {
                "kind": "topic",
                "source": "measured_voice_timeline",
                "start": topic_start,
                "end": topic_end,
            }
        )
    if outro is not None:
        events.append(
            {
                "kind": "outro",
                "source": "measured_voice_chunk",
                "start": float(outro["start"]),
                "end": float(outro["end"]),
            }
        )
    if fmt in {"short", "film", "podcast"} and final_silence is not None:
        events.append(
            {
                "kind": "final_silence",
                "source": (
                    "measured_native_nabra_pause"
                    if str(final_silence.get("provider") or "") == "nabra_native_pause"
                    else "measured_silence_chunk"
                ),
                "start": float(final_silence["start"]),
                "end": float(final_silence["end"]),
            }
        )
    return events


def build_voice_owned_timeline(
    *,
    output_dir: Path,
    narration_path: Path,
    fmt: str,
    require_identity: bool = True,
) -> dict[str, Any]:
    """Build one shared Short/Film/Podcast timeline owned only by measured audio."""
    root = Path(output_dir)
    voice_seconds = _positive(probe_duration(Path(narration_path)), "voice_seconds")
    maximum = safety_max_seconds(fmt)
    voice_provider = ""
    voice_report_path = root / "voice-sections.json"
    if voice_report_path.is_file():
        try:
            voice_report = json.loads(voice_report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            voice_report = {}
        if isinstance(voice_report, Mapping):
            voice_provider = str(voice_report.get("voice_provider") or "").strip()

    base = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "source": CONTRACT_ID,
        "format": fmt,
        "timeline_owner": (
            "measured_nabra_voice"
            if voice_provider == "nabra:af_msa"
            else "measured_charon_voice"
        ),
        "voice_seconds_measured": round(voice_seconds, 3),
        "safety_maximum_seconds": maximum,
        "editorial_target_seconds": None,
        "minimum_editorial_seconds": None,
        "maximum_editorial_seconds": None,
        "time_compression": False,
        "time_extension": False,
        "tts_regeneration_for_duration": False,
        "duration_repair_attempts": 0,
        "provider_calls_added": 0,
    }
    if voice_seconds > maximum + 1e-6:
        blocked = {
            **base,
            "status": "block",
            "reason": "VOICE_EXCEEDS_OPERATIONAL_SAFETY_MAX",
            "section_events": [],
            "identity_events": [],
        }
        raise TimelineFirstError(
            f"VOICE_EXCEEDS_OPERATIONAL_SAFETY_MAX voice={voice_seconds:.3f}s max={maximum:.3f}s",
            report=blocked,
        )

    raw_units = _load_voice_units(root)
    if not raw_units:
        raw_units = _fallback_section_units(root)
    if not raw_units:
        raise TimelineFirstError("TIMELINE_FIRST_AUDIO_UNITS_MISSING")

    raw_total = float(raw_units[-1]["end"])
    measured_units = retime_events(
        raw_units,
        source_seconds=raw_total,
        target_seconds=voice_seconds,
    )
    raw_sections = _section_events(measured_units)
    sections = [
        {
            **item,
            "duration_seconds": round(float(item["end"]) - float(item["start"]), 3),
        }
        for item in raw_sections
    ]
    identities = _identity_events(
        measured_units,
        voice_seconds=voice_seconds,
        require_identity=require_identity,
        fmt=fmt,
    )
    identities = [
        {
            **item,
            "start": round(float(item["start"]), 3),
            "end": round(float(item["end"]), 3),
            "duration_seconds": round(float(item["end"]) - float(item["start"]), 3),
        }
        for item in identities
    ]
    return {
        **base,
        "status": "pass",
        "reason": None,
        "raw_audio_unit_seconds": round(raw_total, 3),
        "retime_scale": round(voice_seconds / raw_total, 6),
        "retime_policy": "measured_audio_units_scaled_to_mastered_voice_only",
        "audio_units": measured_units,
        "section_events": sections,
        "identity_events": identities,
    }


def section_duration_map(report: Mapping[str, Any]) -> dict[str, float]:
    events = report.get("section_events")
    if not isinstance(events, list) or not events:
        raise TimelineFirstError("TIMELINE_FIRST_SECTION_EVENTS_INVALID", report=report)
    result: dict[str, float] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise TimelineFirstError("TIMELINE_FIRST_SECTION_EVENTS_INVALID", report=report)
        section_id = str(event.get("section_id") or "").strip()
        seconds = float(event.get("duration_seconds") or 0.0)
        if not section_id or seconds <= 0:
            raise TimelineFirstError("TIMELINE_FIRST_SECTION_EVENTS_INVALID", report=report)
        result[section_id] = seconds
    return result


def assert_final_matches_voice(
    *,
    final_seconds: float,
    timeline: Mapping[str, Any],
    tolerance_seconds: float = FINAL_DURATION_TOLERANCE_SECONDS,
) -> None:
    voice = _positive(timeline.get("voice_seconds_measured"), "voice_seconds")
    final = _positive(final_seconds, "final_seconds")
    if abs(final - voice) > float(tolerance_seconds):
        raise TimelineFirstError(
            "TIMELINE_FIRST_FINAL_DURATION_MISMATCH "
            f"voice={voice:.3f}s final={final:.3f}s tolerance={tolerance_seconds:.3f}s"
        )
