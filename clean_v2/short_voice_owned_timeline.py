from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .media import probe_duration
from .short_format import (
    SHORT_MAX_SECONDS,
    SHORT_MIN_SECONDS,
    SHORT_SECTION_COUNT,
    SHORT_TARGET_SECONDS,
)

CONTRACT_ID = "clean-v2-short-voice-owned-timeline-v1"


class ShortVoiceTimelineError(RuntimeError):
    """Fail-closed measured-voice timeline contract violation."""

    def __init__(self, message: str, *, report: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.report = dict(report)


def _positive_seconds(value: object, field: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ShortVoiceTimelineError(
            f"VOICE_TIMELINE_INVALID_{field.upper()}",
            report={
                "schema_version": 1,
                "contract_id": CONTRACT_ID,
                "status": "block",
                "reason": f"invalid_{field}",
            },
        ) from None
    if seconds <= 0:
        raise ShortVoiceTimelineError(
            f"VOICE_TIMELINE_INVALID_{field.upper()}",
            report={
                "schema_version": 1,
                "contract_id": CONTRACT_ID,
                "status": "block",
                "reason": f"invalid_{field}",
            },
        )
    return seconds


def retime_events(
    events: Sequence[Mapping[str, Any]],
    *,
    source_seconds: float,
    target_seconds: float,
) -> list[dict[str, Any]]:
    """Scale already-approved section windows onto the measured mastered voice.

    This is deliberately the smallest useful port of the legacy Voice-Owned Timeline
    principle: preserve order and relative section windows, change only timestamps.
    No speech speed change, text rewrite, or provider call occurs here.
    """
    source = _positive_seconds(source_seconds, "source_seconds")
    target = _positive_seconds(target_seconds, "target_seconds")
    if not events:
        raise ShortVoiceTimelineError(
            "VOICE_TIMELINE_EVENTS_MISSING",
            report={
                "schema_version": 1,
                "contract_id": CONTRACT_ID,
                "status": "block",
                "reason": "events_missing",
            },
        )

    scale = target / source
    retimed: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, raw in enumerate(events):
        section_id = str(raw.get("section_id") or "").strip()
        start = float(raw.get("start") or 0.0)
        end = float(raw.get("end") or 0.0)
        if not section_id or end <= start:
            raise ShortVoiceTimelineError(
                "VOICE_TIMELINE_EVENT_INVALID",
                report={
                    "schema_version": 1,
                    "contract_id": CONTRACT_ID,
                    "status": "block",
                    "reason": "event_invalid",
                },
            )
        mapped_start = max(previous_end, start * scale)
        mapped_end = target if index == len(events) - 1 else end * scale
        mapped_end = max(mapped_start, min(target, mapped_end))
        retimed.append(
            {
                "section_id": section_id,
                "start": round(mapped_start, 3),
                "end": round(mapped_end, 3),
                "duration_seconds": round(mapped_end - mapped_start, 3),
            }
        )
        previous_end = mapped_end
    return retimed


def build_short_voice_owned_timeline(
    *,
    output_dir: Path,
    narration_path: Path,
) -> dict[str, Any]:
    """Certify one Charon narration as the timeline owner before visual acquisition.

    The mastered narration is authoritative. The only hard limit is the existing
    Clean V2 Short envelope (7-30s). A voice outside that envelope blocks immediately;
    V1 never retries TTS, rewrites text, or stretches the video past the format cap.
    """
    root = Path(output_dir)
    narration = Path(narration_path)
    voice_seconds = float(probe_duration(narration))

    base = {
        "schema_version": 1,
        "contract_id": CONTRACT_ID,
        "source": CONTRACT_ID,
        "timeline_owner": "measured_charon_voice",
        "voice_seconds_measured": round(voice_seconds, 3),
        "minimum_seconds": SHORT_MIN_SECONDS,
        "target_seconds": SHORT_TARGET_SECONDS,
        "maximum_seconds": SHORT_MAX_SECONDS,
        "time_compression": False,
        "post_speed_factor": 1.0,
        "tts_regeneration_for_duration": False,
        "duration_repair_attempts": 0,
        "provider_calls_added": 0,
        "planning_repair_required": False,
    }

    if voice_seconds > SHORT_MAX_SECONDS + 1e-6:
        report = {
            **base,
            "status": "block",
            "reason": "VOICE_EXCEEDS_SHORT_MAX",
            "planning_repair_required": True,
            "section_events": [],
        }
        raise ShortVoiceTimelineError(
            "VOICE_EXCEEDS_SHORT_MAX "
            f"voice={voice_seconds:.3f}s max={SHORT_MAX_SECONDS:.3f}s "
            "planning_repair_required=true",
            report=report,
        )

    if voice_seconds < SHORT_MIN_SECONDS - 1e-6:
        report = {
            **base,
            "status": "block",
            "reason": "VOICE_BELOW_SHORT_MIN",
            "planning_repair_required": True,
            "section_events": [],
        }
        raise ShortVoiceTimelineError(
            "VOICE_BELOW_SHORT_MIN "
            f"voice={voice_seconds:.3f}s min={SHORT_MIN_SECONDS:.3f}s "
            "planning_repair_required=true",
            report=report,
        )

    raw_events: list[dict[str, Any]] = []
    cursor = 0.0
    for index in range(1, SHORT_SECTION_COUNT + 1):
        section_audio = root / "audio" / f"{index:02d}.wav"
        if not section_audio.is_file() or section_audio.stat().st_size <= 0:
            report = {
                **base,
                "status": "block",
                "reason": "VOICE_TIMELINE_SECTION_AUDIO_MISSING",
                "missing_section_index": index,
                "section_events": [],
            }
            raise ShortVoiceTimelineError(
                f"VOICE_TIMELINE_SECTION_AUDIO_MISSING index={index}",
                report=report,
            )
        section_seconds = float(probe_duration(section_audio))
        raw_events.append(
            {
                "section_id": f"s{index}",
                "start": cursor,
                "end": cursor + section_seconds,
            }
        )
        cursor += section_seconds

    if cursor <= 0:
        report = {
            **base,
            "status": "block",
            "reason": "VOICE_TIMELINE_SECTION_AUDIO_INVALID",
            "section_events": [],
        }
        raise ShortVoiceTimelineError(
            "VOICE_TIMELINE_SECTION_AUDIO_INVALID",
            report=report,
        )

    events = retime_events(
        raw_events,
        source_seconds=cursor,
        target_seconds=voice_seconds,
    )
    return {
        **base,
        "status": "pass",
        "reason": None,
        "raw_section_audio_seconds": round(cursor, 3),
        "retime_scale": round(voice_seconds / cursor, 6),
        "retime_policy": "section_audio_windows_scaled_to_mastered_voice",
        "section_events": events,
    }


def section_duration_map(report: Mapping[str, Any]) -> dict[str, float]:
    events = report.get("section_events")
    if not isinstance(events, list) or len(events) != SHORT_SECTION_COUNT:
        raise ShortVoiceTimelineError(
            "VOICE_TIMELINE_SECTION_EVENTS_INVALID",
            report=dict(report),
        )
    result: dict[str, float] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise ShortVoiceTimelineError(
                "VOICE_TIMELINE_SECTION_EVENTS_INVALID",
                report=dict(report),
            )
        section_id = str(event.get("section_id") or "").strip()
        try:
            seconds = float(event.get("duration_seconds"))
        except (TypeError, ValueError):
            seconds = 0.0
        if not section_id or seconds <= 0:
            raise ShortVoiceTimelineError(
                "VOICE_TIMELINE_SECTION_EVENTS_INVALID",
                report=dict(report),
            )
        result[section_id] = seconds
    return result
