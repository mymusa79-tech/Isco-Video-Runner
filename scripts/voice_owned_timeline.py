from __future__ import annotations

from typing import Any


CONTRACT_ID = "voice-owned-timeline-v1"
NATURAL_POST_SPEED_FACTOR = 1.0
NATURAL_TAIL_SECONDS = 0.35
HYBRID_SECONDS_PER_VISIBLE_BEAT = 1.80
SOURCE_DERIVED_MAX_HOLD_SECONDS = 0.75


class VoiceOwnedTimelineError(RuntimeError):
    pass


def _positive_float(value: object, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return parsed if parsed > 0 else fallback


def build_voice_owned_timeline(
    *,
    voice_seconds: float,
    source_visual_seconds: float,
    minimum_seconds: float,
    maximum_seconds: float,
    mode: str,
    visible_beat_count: int,
    source_derived_from_long: bool,
) -> dict[str, Any]:
    """Make natural narration authoritative without post-hoc voice time compression.

    Voice-led Shorts follow measured narration plus a small natural tail. Hybrid Shorts
    retain enough silent visual/readability room for approved on-screen beats. Duration
    estimates may provision media earlier, but only measured voice duration certifies the
    final timeline here.
    """
    voice_seconds = _positive_float(voice_seconds, 0.0)
    source_visual_seconds = _positive_float(source_visual_seconds, 0.0)
    minimum_seconds = _positive_float(minimum_seconds, 7.0)
    maximum_seconds = _positive_float(maximum_seconds, 25.0)
    if voice_seconds <= 0 or source_visual_seconds <= 0:
        raise VoiceOwnedTimelineError("VOICE_TIMELINE_MEASUREMENT_MISSING")
    if minimum_seconds > maximum_seconds:
        raise VoiceOwnedTimelineError("VOICE_TIMELINE_DURATION_RANGE_INVALID")
    if mode not in {"voice_led", "hybrid"}:
        raise VoiceOwnedTimelineError("VOICE_TIMELINE_MODE_INVALID")

    natural_voice_target = voice_seconds + NATURAL_TAIL_SECONDS
    semantic_floor = minimum_seconds
    if mode == "hybrid":
        semantic_floor = max(
            minimum_seconds,
            max(2, int(visible_beat_count or 0)) * HYBRID_SECONDS_PER_VISIBLE_BEAT,
        )
    target_seconds = max(minimum_seconds, natural_voice_target, semantic_floor)
    if target_seconds > maximum_seconds + 1e-6:
        raise VoiceOwnedTimelineError(
            "VOICE_TIMELINE_EXCEEDS_SHORT_MAX:"
            f"voice={voice_seconds:.3f}s target={target_seconds:.3f}s max={maximum_seconds:.3f}s;"
            "planning_repair_required=true"
        )

    delta = target_seconds - source_visual_seconds
    if source_derived_from_long and delta > SOURCE_DERIVED_MAX_HOLD_SECONDS + 1e-6:
        raise VoiceOwnedTimelineError(
            "SOURCE_DERIVED_VISUAL_BUDGET_TOO_SHORT:"
            f"source={source_visual_seconds:.3f}s target={target_seconds:.3f}s delta={delta:.3f}s;"
            "source_safe_reprovision_required=true"
        )

    return {
        "contract_id": CONTRACT_ID,
        "timeline_owner": "voice" if mode == "voice_led" else "voice_plus_visible_beats",
        "voice_seconds_measured": round(voice_seconds, 3),
        "source_visual_seconds": round(source_visual_seconds, 3),
        "target_seconds": round(target_seconds, 3),
        "timeline_adjustment_seconds": round(delta, 3),
        "minimum_seconds": round(minimum_seconds, 3),
        "maximum_seconds": round(maximum_seconds, 3),
        "mode": mode,
        "visible_beat_count": int(visible_beat_count or 0),
        "source_derived_from_long": bool(source_derived_from_long),
        "post_speed_factor": NATURAL_POST_SPEED_FACTOR,
        "time_compression": False,
        "natural_tail_seconds": NATURAL_TAIL_SECONDS,
        "measured_voice_is_authoritative": True,
    }


def retime_events(
    events: list[dict[str, Any]],
    *,
    source_seconds: float,
    target_seconds: float,
) -> list[dict[str, Any]]:
    """Scale approved semantic windows to the measured final timeline without reordering."""
    source_seconds = _positive_float(source_seconds, 0.0)
    target_seconds = _positive_float(target_seconds, 0.0)
    if source_seconds <= 0 or target_seconds <= 0:
        raise VoiceOwnedTimelineError("VOICE_TIMELINE_EVENT_DURATION_INVALID")
    if not events:
        raise VoiceOwnedTimelineError("VOICE_TIMELINE_EVENTS_MISSING")

    scale = target_seconds / source_seconds
    retimed: list[dict[str, Any]] = []
    previous_end = 0.0
    for index, raw in enumerate(events):
        if not isinstance(raw, dict):
            raise VoiceOwnedTimelineError("VOICE_TIMELINE_EVENT_MALFORMED")
        item = dict(raw)
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
        except (TypeError, ValueError) as exc:
            raise VoiceOwnedTimelineError("VOICE_TIMELINE_EVENT_TIME_INVALID") from exc
        start = max(previous_end, start * scale)
        end = min(target_seconds, max(start + 0.05, end * scale))
        if index == len(events) - 1:
            end = target_seconds
        item["start"] = round(start, 3)
        item["end"] = round(end, 3)
        retimed.append(item)
        previous_end = end
    return retimed


def provision_source_derived_visual_seconds(beat_texts: list[str]) -> float:
    """Provision source-safe media generously; this estimate never certifies voice fit."""
    texts = [str(text or "").strip() for text in beat_texts if str(text or "").strip()]
    words = sum(len(text.split()) for text in texts)
    pauses = max(0, len(texts) - 1) * 0.45
    estimate = (words / 1.45) + pauses + 1.25
    return round(min(24.5, max(12.0, estimate)), 2)
