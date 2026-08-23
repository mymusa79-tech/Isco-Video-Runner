from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.branding import DEFAULT_OUTRO_SECONDS, normalize_outro_seconds
from isco_video_agent.media.ffmpeg import duration
from isco_video_agent.security import secret_free_subprocess_env


SCHEMA_VERSION = 2
RAMP_SECONDS = 1.6
MAX_ABS_ADJUSTMENT_DB = 1.5
OUTRO_ADJUSTMENT_DB = -1.5
_PACING_ROLE_DB = {
    "linger": -0.9,
    "steady": 0.0,
    "build": 0.6,
    "accelerate": 1.1,
    "release": -0.8,
}


class NarrativeMusicDynamicsError(RuntimeError):
    """Internal validation error for optional narrative music dynamics."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split()).casefold()


def _db_to_gain(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def _clamp_db(value: float) -> float:
    return max(-MAX_ABS_ADJUSTMENT_DB, min(MAX_ABS_ADJUSTMENT_DB, float(value)))


def _target_db(role: str) -> float:
    return round(_clamp_db(float(_PACING_ROLE_DB.get(_clean(role), 0.0))), 3)


def _read_timeline(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise NarrativeMusicDynamicsError(f"cannot_read_visual_timeline:{type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise NarrativeMusicDynamicsError("visual_timeline_must_be_object")
    return data


def _adaptive_pacing_available(timeline: dict[str, Any]) -> bool:
    shots = timeline.get("final_cut_visuals")
    if not isinstance(shots, list):
        return False
    for shot in shots:
        if not isinstance(shot, dict):
            continue
        pacing = shot.get("adaptive_pacing")
        role = _clean(pacing.get("role")) if isinstance(pacing, dict) else ""
        if role in _PACING_ROLE_DB:
            return True
    return False


def _raw_segments(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    shots = timeline.get("final_cut_visuals")
    if not isinstance(shots, list) or not shots:
        raise NarrativeMusicDynamicsError("timeline_has_no_final_cut_visuals")

    result: list[dict[str, Any]] = []
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            raise NarrativeMusicDynamicsError("final_cut_visual_contains_non_object")
        try:
            start = float(shot.get("start_seconds"))
            end = float(shot.get("end_seconds"))
        except (TypeError, ValueError) as exc:
            raise NarrativeMusicDynamicsError("final_cut_visual_has_invalid_timing") from exc
        if start < 0 or end <= start:
            raise NarrativeMusicDynamicsError("final_cut_visual_has_non_positive_duration")

        pacing = shot.get("adaptive_pacing")
        role = _clean(pacing.get("role")) if isinstance(pacing, dict) else ""
        if role in _PACING_ROLE_DB:
            adjustment_db = _target_db(role)
            derivation = "m7_adaptive_pacing_role"
        else:
            role = "neutral_missing_pacing_role"
            adjustment_db = 0.0
            derivation = "neutral_fallback_no_m7_pacing_role"

        result.append(
            {
                "index": index,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "pacing_role": role,
                "adjustment_db": round(adjustment_db, 3),
                "derivation": derivation,
            }
        )
    result.sort(key=lambda item: (item["start_seconds"], item["end_seconds"], item["index"]))
    return result


def _coverage_schedule(
    timeline: dict[str, Any],
    *,
    narrative_seconds: float,
    total_seconds: float,
) -> list[dict[str, Any]]:
    if narrative_seconds <= 0 or total_seconds <= 0 or total_seconds + 1e-6 < narrative_seconds:
        raise NarrativeMusicDynamicsError("invalid_audio_duration_for_music_dynamics")
    timeline_seconds = float(timeline.get("duration_seconds") or 0.0)
    if timeline_seconds <= 0:
        raise NarrativeMusicDynamicsError("timeline_duration_missing")
    if abs(timeline_seconds - narrative_seconds) > 1.0:
        raise NarrativeMusicDynamicsError(
            f"timeline_narration_duration_mismatch:{timeline_seconds:.3f}:{narrative_seconds:.3f}"
        )

    raw = _raw_segments(timeline)
    if not raw:
        return []

    schedule: list[dict[str, Any]] = []
    cursor = 0.0
    tolerance = 0.05
    for segment in raw:
        start = max(0.0, min(narrative_seconds, float(segment["start_seconds"])))
        end = max(0.0, min(narrative_seconds, float(segment["end_seconds"])))
        if end <= start:
            continue
        if start < cursor - tolerance:
            raise NarrativeMusicDynamicsError("final_cut_visual_timing_overlaps")
        if start > cursor + tolerance:
            schedule.append(
                {
                    "start_seconds": round(cursor, 3),
                    "end_seconds": round(start, 3),
                    "pacing_role": "timeline_gap_neutral",
                    "adjustment_db": 0.0,
                    "derivation": "neutral_gap_fill",
                }
            )
        elif start < cursor:
            start = cursor
        schedule.append({**segment, "start_seconds": round(start, 3), "end_seconds": round(end, 3)})
        cursor = max(cursor, end)

    if cursor < narrative_seconds - tolerance:
        schedule.append(
            {
                "start_seconds": round(cursor, 3),
                "end_seconds": round(narrative_seconds, 3),
                "pacing_role": "timeline_tail_neutral",
                "adjustment_db": 0.0,
                "derivation": "neutral_tail_fill",
            }
        )
    if total_seconds > narrative_seconds + 0.01:
        schedule.append(
            {
                "start_seconds": round(narrative_seconds, 3),
                "end_seconds": round(total_seconds, 3),
                "pacing_role": "outro_quiet_tail",
                "adjustment_db": OUTRO_ADJUSTMENT_DB,
                "derivation": "professional_ending_guard",
            }
        )

    # M7 may split one semantic scene into several shots. Do not pump the music on
    # every edit: adjacent shots with the same role collapse into one musical phrase.
    merged: list[dict[str, Any]] = []
    for segment in schedule:
        if segment["end_seconds"] <= segment["start_seconds"]:
            continue
        if (
            merged
            and abs(float(merged[-1]["adjustment_db"]) - float(segment["adjustment_db"])) < 0.001
            and abs(float(merged[-1]["end_seconds"]) - float(segment["start_seconds"])) <= tolerance
        ):
            merged[-1]["end_seconds"] = segment["end_seconds"]
            if merged[-1]["pacing_role"] != segment["pacing_role"]:
                merged[-1]["pacing_role"] = "mixed_same_gain"
            continue
        merged.append(dict(segment))
    return merged


def _ramp_pieces(schedule: list[dict[str, Any]], *, ramp_seconds: float = RAMP_SECONDS) -> list[dict[str, float]]:
    if not schedule:
        return []
    pieces: list[dict[str, float]] = []
    current_start = float(schedule[0]["start_seconds"])
    current_gain = _db_to_gain(float(schedule[0]["adjustment_db"]))
    for index in range(len(schedule) - 1):
        left = schedule[index]
        right = schedule[index + 1]
        boundary = float(left["end_seconds"])
        next_gain = _db_to_gain(float(right["adjustment_db"]))
        available_left = max(0.0, boundary - current_start)
        available_right = max(0.0, float(right["end_seconds"]) - boundary)
        ramp = min(float(ramp_seconds), available_left * 0.5, available_right * 0.5)
        if ramp < 0.05 or abs(next_gain - current_gain) < 1e-6:
            if boundary > current_start:
                pieces.append(
                    {
                        "start": current_start,
                        "end": boundary,
                        "gain_start": current_gain,
                        "gain_end": current_gain,
                    }
                )
            current_start = boundary
            current_gain = next_gain
            continue

        ramp_start = boundary - ramp / 2.0
        ramp_end = boundary + ramp / 2.0
        if ramp_start > current_start:
            pieces.append(
                {
                    "start": current_start,
                    "end": ramp_start,
                    "gain_start": current_gain,
                    "gain_end": current_gain,
                }
            )
        pieces.append(
            {
                "start": ramp_start,
                "end": ramp_end,
                "gain_start": current_gain,
                "gain_end": next_gain,
            }
        )
        current_start = ramp_end
        current_gain = next_gain

    final_end = float(schedule[-1]["end_seconds"])
    if final_end > current_start:
        pieces.append(
            {
                "start": current_start,
                "end": final_end,
                "gain_start": current_gain,
                "gain_end": current_gain,
            }
        )
    return pieces


def _ffmpeg_volume_expression(pieces: list[dict[str, float]]) -> str:
    if not pieces:
        return "1.0"
    formulas: list[tuple[float, str]] = []
    for piece in pieces:
        start = float(piece["start"])
        end = float(piece["end"])
        a = float(piece["gain_start"])
        b = float(piece["gain_end"])
        if end <= start or abs(a - b) < 1e-8:
            formula = f"{a:.8f}"
        else:
            formula = f"({a:.8f}+({b - a:.8f})*(t-{start:.3f})/{end - start:.3f})"
        formulas.append((end, formula))
    expression = formulas[-1][1]
    for end, formula in reversed(formulas[:-1]):
        expression = f"if(lt(t,{end:.3f}),{formula},{expression})"
    return expression


def _render_dynamic_music(music: Path, dest: Path, *, total_seconds: float, expression: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    filter_arg = f"volume='{expression}':eval=frame,aresample=48000"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostats",
            "-stream_loop",
            "-1",
            "-i",
            str(music),
            "-t",
            f"{total_seconds:.3f}",
            "-af",
            filter_arg,
            "-c:a",
            "pcm_s16le",
            str(dest),
        ],
        check=True,
        env=secret_free_subprocess_env(),
        capture_output=True,
        text=True,
    )
    if not dest.is_file() or dest.stat().st_size < 1024:
        raise NarrativeMusicDynamicsError("dynamic_music_render_empty")


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _fallback_report(*, reason: str, timeline_path: Path, source_music: Path) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "fallback_original_music",
        "reason": reason[:320],
        "source_timeline": timeline_path.name,
        "source_music": source_music.name,
        "ai_calls_added": 0,
        "music_bed_only": True,
        "production_blocked": False,
        "sidechain_ducking_authority": "engine_mux_unchanged",
        "final_loudness_limiter_authority": "engine_mux_unchanged",
        "narration_authority_changed": False,
        "rights_provenance_changed": False,
    }


def install_narrative_music_dynamics() -> None:
    """Bind restrained M7 pacing-role music movement before the existing mux.

    This is optional editorial polish, never a new production authority. M7 adaptive
    pacing decides the musical contour; Human Editorial Intent does not directly drive
    gain. Existing sidechain ducking, two-pass loudness, limiter, narration, track
    selection and rights provenance stay authoritative. Any polish-specific failure
    falls back to the original music bed rather than blocking production.
    """
    current = orchestrator.mux
    if getattr(current, "_isco_narrative_music_dynamics", False):
        return

    def wrapped(video: Path, narration: Path | None, output: Path, music: Path | None = None, **kwargs):
        output = Path(output)
        output_dir = output.parent
        report_path = output_dir / "narrative-music-dynamics.json"
        timeline_path = output_dir / "visual-timeline.json"
        if narration is None or music is None or not timeline_path.is_file():
            return current(video, narration, output, music=music, **kwargs)

        source_music = Path(music)
        dynamic_music = output_dir / "narrative-music-dynamics.wav"
        try:
            timeline = _read_timeline(timeline_path)
            if not _adaptive_pacing_available(timeline):
                _write_report(
                    report_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "not_applicable",
                        "reason": "timeline_has_no_m7_adaptive_pacing_roles",
                        "ai_calls_added": 0,
                        "production_blocked": False,
                        "renderer_authority_changed": False,
                    },
                )
                return current(video, narration, output, music=music, **kwargs)

            narrative_seconds = duration(Path(narration))
            requested_outro = kwargs.get("outro_seconds", DEFAULT_OUTRO_SECONDS)
            effective_outro = normalize_outro_seconds(max(0.0, float(requested_outro)))
            total_seconds = narrative_seconds + effective_outro
            schedule = _coverage_schedule(
                timeline,
                narrative_seconds=narrative_seconds,
                total_seconds=total_seconds,
            )
            if not schedule:
                raise NarrativeMusicDynamicsError("adaptive_pacing_produced_no_music_schedule")
            pieces = _ramp_pieces(schedule)
            expression = _ffmpeg_volume_expression(pieces)
            _render_dynamic_music(
                source_music,
                dynamic_music,
                total_seconds=total_seconds,
                expression=expression,
            )
        except Exception as exc:
            dynamic_music.unlink(missing_ok=True)
            _write_report(
                report_path,
                _fallback_report(
                    reason=f"pre_mux:{type(exc).__name__}:{exc}",
                    timeline_path=timeline_path,
                    source_music=source_music,
                ),
            )
            return current(video, narration, output, music=music, **kwargs)

        _write_report(
            report_path,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "applied",
                "mode": "m7_adaptive_pacing_music_dynamics",
                "source_timeline": timeline_path.name,
                "source_music": source_music.name,
                "narrative_seconds": round(narrative_seconds, 3),
                "total_music_seconds": round(total_seconds, 3),
                "ramp_seconds": RAMP_SECONDS,
                "max_abs_adjustment_db": MAX_ABS_ADJUSTMENT_DB,
                "role_adjustments_db": dict(_PACING_ROLE_DB),
                "segments": schedule,
                "ai_calls_added": 0,
                "music_bed_only": True,
                "production_blocked": False,
                "sidechain_ducking_authority": "engine_mux_unchanged",
                "final_loudness_limiter_authority": "engine_mux_unchanged",
                "narration_authority_changed": False,
                "rights_provenance_changed": False,
                "transient_music_render_deleted_after_mux": True,
            },
        )
        try:
            return current(video, narration, output, music=dynamic_music, **kwargs)
        except Exception as exc:
            # One bounded retry without the optional polish proves whether the new bed
            # caused the failure. If the baseline mux also fails, propagate that real
            # production failure rather than pretending the polish recovered it.
            dynamic_music.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            _write_report(
                report_path,
                _fallback_report(
                    reason=f"dynamic_mux_retry_original:{type(exc).__name__}:{exc}",
                    timeline_path=timeline_path,
                    source_music=source_music,
                ),
            )
            return current(video, narration, output, music=music, **kwargs)
        finally:
            dynamic_music.unlink(missing_ok=True)

    wrapped._isco_narrative_music_dynamics = True
    wrapped._isco_narrative_music_dynamics_original = current
    orchestrator.mux = wrapped
