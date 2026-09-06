from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import env, load_channel_config, secret
from isco_video_agent.media.ffmpeg import duration, measure_audio_loudness, probe
from isco_video_agent.tts_budget import TtsBudget, TtsCircuit

from scripts.short_cinematic_director import apply_short_sfx, upgrade_short_cinematic
from scripts.short_voice_feasibility import RUNTIME_MAX_SPEED, build_voice_projection
from scripts.voice_mesh import consume_voice_provenance


VOICE_MODE_BY_TEMPLATE = {
    "why_reframe": "hybrid",
    "inner_dialogue": "voice_led",
    "micro_story": "voice_led",
    "quote_reflection": "hybrid",
}
MIX_DURATION_TOLERANCE_SECONDS = 0.15


def decide_voice_mode(template: str) -> str:
    try:
        return VOICE_MODE_BY_TEMPLATE[str(template).strip()]
    except KeyError as exc:
        raise RuntimeError("Short Voice V2 received unsupported template") from exc


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _voice_script(events: list[dict[str, Any]], mode: str) -> str:
    texts = [_clean(item.get("text")) for item in events if isinstance(item, dict) and _clean(item.get("text"))]
    if len(texts) < 2:
        raise RuntimeError("Short Voice V2 requires at least two semantic beats")
    if mode == "voice_led":
        chosen = texts
    elif mode == "hybrid":
        chosen = [texts[0], texts[-1]]
    else:
        raise RuntimeError("Short Voice V2 received unsupported voice mode")
    return "، ".join(text.rstrip(" .،!?؟") for text in chosen) + "."


def _final_duration(path: Path) -> float:
    info = probe(path)
    try:
        value = float((info.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        raise RuntimeError("Short Voice V2 cannot resolve final duration")
    return value


def _has_audio(path: Path) -> bool:
    info = probe(path)
    streams = info.get("streams") if isinstance(info, dict) else []
    return any(isinstance(item, dict) and item.get("codec_type") == "audio" for item in (streams or []))


def _stream_duration(streams: list[dict[str, Any]]) -> float:
    if not streams:
        return 0.0
    try:
        return float(streams[0].get("duration", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _fit_voice_to_video(voice_path: Path, final_seconds: float) -> Path:
    voice_seconds = float(duration(voice_path))
    if voice_seconds <= final_seconds - 0.15:
        return voice_path
    speed = voice_seconds / max(0.1, final_seconds - 0.15)
    if speed > RUNTIME_MAX_SPEED:
        raise RuntimeError(
            f"Short Voice V2 narration is too dense for the approved duration: speed_required={speed:.3f}"
        )
    fitted = voice_path.with_name("short-voice-v2-fitted.wav")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(voice_path),
            "-filter:a", f"atempo={speed:.5f}",
            "-ar", "48000", "-ac", "1", str(fitted),
        ],
        check=True,
        capture_output=True,
    )
    if not fitted.is_file() or fitted.stat().st_size <= 1024:
        raise RuntimeError("Short Voice V2 failed to fit narration to final duration")
    return fitted


def _mix_voice(final_path: Path, voice_path: Path, output: Path) -> Path:
    """Add Short voice without changing the already-approved visual timeline duration."""
    source_seconds = _final_duration(final_path)
    if _has_audio(final_path):
        filter_complex = (
            "[0:a:0]volume=0.24[bed];"
            "[1:a:0]volume=1.0[voice];"
            "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
        command = [
            "ffmpeg", "-y", "-i", str(final_path), "-i", str(voice_path),
            "-filter_complex", filter_complex,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-movflags", "+faststart", str(output),
        ]
    else:
        # Historical `-shortest` made a shorter narration authoritative over the video
        # timeline and could truncate a valid 15s visual to an 8s voice. Pad the audio
        # locally and explicitly stop at the immutable source-video duration instead.
        command = [
            "ffmpeg", "-y", "-i", str(final_path), "-i", str(voice_path),
            "-filter_complex", "[1:a:0]loudnorm=I=-16:TP=-1.5:LRA=11,apad[aout]",
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-t", f"{source_seconds:.3f}", "-movflags", "+faststart", str(output),
        ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Short Voice V2 did not produce a usable final master")
    rendered_seconds = _final_duration(output)
    if abs(rendered_seconds - source_seconds) > MIX_DURATION_TOLERANCE_SECONDS:
        raise RuntimeError(
            "Short Voice V2 voice mix changed approved visual duration: "
            f"source={source_seconds:.3f}s mixed={rendered_seconds:.3f}s "
            f"tolerance={MIX_DURATION_TOLERANCE_SECONDS:.3f}s"
        )
    return output


def _refresh_quality_final(root: Path, final_path: Path) -> dict[str, Any]:
    """Re-measure the exact finished Short bytes that Final Critic and Gold inspect."""
    quality_path = root / "quality-final.json"
    try:
        previous = json.loads(quality_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Short Voice V2 requires valid quality-final.json before refresh") from exc
    if not isinstance(previous, dict):
        raise RuntimeError("Short Voice V2 quality-final.json must be an object")

    info = probe(final_path)
    streams = info.get("streams") if isinstance(info, dict) else []
    video_streams = [x for x in (streams or []) if isinstance(x, dict) and x.get("codec_type") == "video"]
    audio_streams = [x for x in (streams or []) if isinstance(x, dict) and x.get("codec_type") == "audio"]
    if len(video_streams) != 1 or len(audio_streams) != 1:
        raise RuntimeError("Short Voice V2 final master must contain exactly one video and one audio stream")

    duration_seconds = _final_duration(final_path)
    minimum = float(previous.get("duration_expected_min") or 7.0)
    maximum = float(previous.get("duration_expected_max") or 25.0)
    duration_ok = minimum <= duration_seconds <= maximum
    audio_measurement = measure_audio_loudness(final_path)
    target_lufs = float(load_channel_config()["quality"].get("audio_lufs", -16))
    audio_ok = (
        abs(float(audio_measurement["integrated_lufs"]) - target_lufs) <= 2.5
        and float(audio_measurement["true_peak_dbtp"]) <= -1.0
    )
    video_seconds = _stream_duration(video_streams)
    audio_seconds = _stream_duration(audio_streams)
    av_delta = abs(video_seconds - audio_seconds)
    av_sync_limit = float(load_channel_config()["quality"].get("av_sync_max_delta_seconds", 1.0))
    coverage = (audio_seconds / video_seconds) if video_seconds > 0 else 0.0
    av_sync_ok = av_delta <= av_sync_limit

    refreshed = {
        **previous,
        "duration_seconds": duration_seconds,
        "duration_ok": duration_ok,
        "audio_measurement": audio_measurement,
        "audio_ok": audio_ok,
        "video_stream_duration": video_seconds,
        "audio_stream_duration": audio_seconds,
        "av_delta_seconds": av_delta,
        "audio_coverage_ratio": coverage,
        "av_sync_ok": av_sync_ok,
        "video_streams": len(video_streams),
        "audio_streams": len(audio_streams),
        "short_voice_v2_refresh": True,
        "short_cinematic_final_refresh": (root / "short-visual-timeline.json").is_file(),
        "short_sfx_final_refresh": (root / "short-sfx-plan.json").is_file(),
        "quality_measurement_stage": "post_short_finishing_pre_gold",
        "audio_target_lufs": target_lufs,
    }
    quality_path.write_text(json.dumps(refreshed, ensure_ascii=False, indent=2), encoding="utf-8")
    if not duration_ok:
        raise RuntimeError("Short Voice V2 final duration failed refreshed quality-final gate")
    if not audio_ok:
        raise RuntimeError("Short Voice V2 final loudness/true-peak failed refreshed quality-final gate")
    if not av_sync_ok:
        raise RuntimeError("Short Voice V2 final A/V sync failed refreshed quality-final gate")
    return refreshed


def _synthesize_voice(
    *,
    root: Path,
    ledger: BudgetLedger,
    task_id: str,
    gemini: str,
    transcript: str,
    model: str,
    voice: str,
    template: str,
    mode: str,
    strategy: str,
) -> tuple[Path, dict[str, Any]]:
    voice_path = root / "short-voice-v2.wav"
    orchestrator._synthesize_tts_section(
        ledger,
        TtsCircuit(),
        TtsBudget(max_extra_attempts=1),
        task_id=task_id,
        api_key=gemini,
        transcript=transcript,
        output=voice_path,
        model=model,
        voice=voice,
        style=(
            "Warm, mature, natural Modern Standard Arabic. Calm confidence; no announcer tone. "
            f"Short template: {template}; delivery mode: {mode}; projection: {strategy}."
        ),
    )
    return voice_path, consume_voice_provenance(voice_path)


def _record_voice_rights(
    root: Path,
    *,
    provider: str,
    fallback_used: object,
    model: str,
    voice: str,
) -> None:
    rights_path = root / "rights-manifest.json"
    try:
        rights = json.loads(rights_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Short Voice V2 requires valid rights-manifest.json") from exc
    if not isinstance(rights, dict):
        raise RuntimeError("Short Voice V2 rights manifest must be an object")
    rights["short_voice_v2"] = {
        "generated": True,
        "provider": provider,
        "fallback_used": fallback_used,
        "requested_model": model,
        "requested_voice": voice,
        "source": "template_driven_semantic_beats",
        "commercial_release_provenance_recorded": True,
    }
    rights_path.write_text(json.dumps(rights, ensure_ascii=False, indent=2), encoding="utf-8")


def apply_short_voice_v2(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: BudgetLedger,
) -> dict[str, Any]:
    """Generate voice, Short-native cinematic finishing and SFX before authoritative QC/Gold."""
    root = Path(output_dir)
    scope = str(control_request.get("approval_scope") or "").strip()
    if scope not in {"short_only", "short_sibling"}:
        return pre_gold

    template = str(pre_gold.get("short_template") or "").strip()
    mode = decide_voice_mode(template)
    events = list(pre_gold.get("timed_text_events") or [])
    final_path = root / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Short Voice V2 requires final.mp4")
    final_seconds = _final_duration(final_path)

    # Duration ownership belongs here, before any TTS provider call. Keep all approved
    # on-screen beats, but speak only the richest semantic projection that fits within
    # the natural-duration budget. The runtime 1.20x ceiling remains a final hard guard.
    projection = build_voice_projection(events, mode, final_seconds=final_seconds)
    transcript = str(projection["transcript"])

    gemini = secret("GEMINI_API_KEY")
    if not gemini:
        raise RuntimeError("Short Voice V2 requires Gemini key for Voice Mesh primary")
    model = env("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview") or "gemini-3.1-flash-tts-preview"
    voice = env("GEMINI_TTS_VOICE", "Gacrux") or "Gacrux"
    voice_path, provenance = _synthesize_voice(
        root=root, ledger=ledger, task_id="SHORT_VOICE_V2", gemini=gemini,
        transcript=transcript, model=model, voice=voice, template=template, mode=mode,
        strategy=str(projection["strategy"]),
    )
    dense_retry_used = False
    try:
        fitted_voice = _fit_voice_to_video(voice_path, final_seconds)
    except RuntimeError as exc:
        if "too dense for the approved duration" not in str(exc):
            raise
        # Run #196: build_voice_projection()'s pre-synthesis word-rate estimate can
        # undershoot Gemini's real pacing (natural pauses on rhetorical turns push
        # actual duration above the estimate even though it fit the padded planning
        # budget). Bounded, once-only recovery: fall back to the next-richest
        # projection not yet tried and re-synthesize exactly once before failing
        # closed - never an unbounded retry loop, and the runtime 1.20x ceiling
        # itself is never relaxed; a genuinely undeliverable video still fails.
        projection = build_voice_projection(
            events, mode, final_seconds=final_seconds,
            exclude_index_sets=[projection["spoken_beat_indexes"]],
        )
        transcript = str(projection["transcript"])
        voice_path, provenance = _synthesize_voice(
            root=root, ledger=ledger, task_id="SHORT_VOICE_V2_RETRY", gemini=gemini,
            transcript=transcript, model=model, voice=voice, template=template, mode=mode,
            strategy=str(projection["strategy"]),
        )
        fitted_voice = _fit_voice_to_video(voice_path, final_seconds)
        dense_retry_used = True
    voiced = root / "final-short-voice-v2.mp4"
    _mix_voice(final_path, fitted_voice, voiced)
    shutil.move(str(voiced), str(final_path))

    provider = str(provenance.get("provider") or "unknown")
    fallback_used = provenance.get("fallback_used")

    # Only the immutable Telegram/control request with kind=short activates the new
    # finishing layer. Unit callers and non-control compatibility paths retain the
    # historical Voice V2 behavior rather than accidentally issuing media/provider work.
    updated = dict(pre_gold)
    if control_request.get("kind") == "short":
        updated = upgrade_short_cinematic(root, control_request, updated, ledger=ledger)
        updated = apply_short_sfx(root, updated)

    # All byte mutations are complete before this authoritative measurement. Gold and
    # Final Critic therefore inspect exactly the multi-shot/SFX/voice master that ships.
    quality = _refresh_quality_final(root, final_path)
    _record_voice_rights(
        root,
        provider=provider,
        fallback_used=fallback_used,
        model=model,
        voice=voice,
    )

    compensation = dict(updated.get("compensation") or {})
    compensation.update(
        {
            "voice_generated": True,
            "voice_mode": mode,
            "voice_template_driven": True,
            "voice_scope": scope,
            "voice_provider": provider,
            "voice_fallback_used": fallback_used,
            "voice_task_id": "SHORT_VOICE_V2_RETRY" if dense_retry_used else "SHORT_VOICE_V2",
            "voice_duration_preflight": True,
            "voice_projection_strategy": projection.get("strategy"),
            "voice_original_beat_count": projection.get("original_beat_count"),
            "voice_spoken_beat_count": projection.get("spoken_beat_count"),
            "voice_omitted_beat_indexes": projection.get("omitted_beat_indexes"),
            "voice_estimated_natural_seconds": projection.get("estimated_natural_seconds"),
            "voice_planning_budget_seconds": projection.get("planning_budget_seconds"),
            "voice_runtime_max_speed_unchanged": projection.get("runtime_max_speed_unchanged"),
            "voice_dense_retry_used": dense_retry_used,
            "gemini_provider_attempt_cap": 1,
            "piper_local_fallback": True,
            "extra_text_ai_calls": 0,
            "existing_audio_preserved": True,
            "quality_final_refreshed_after_voice": True,
            "quality_final_refreshed_after_short_finishing": True,
        }
    )
    updated["compensation"] = compensation
    updated["voice"] = {
        "mode": mode,
        "template": template,
        "scope": scope,
        "source_derived_from_long": scope == "short_sibling",
        "transcript": transcript,
        "projection": projection,
        "provider": provider,
        "fallback_used": fallback_used,
        "generated_before_authoritative_final_master_qc": True,
        "quality_final_stage": quality.get("quality_measurement_stage"),
        "rights_provenance_recorded": True,
    }
    (root / "short-intelligence-pre-gold.json").write_text(
        json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated
