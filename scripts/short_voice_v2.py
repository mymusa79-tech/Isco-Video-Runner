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

from scripts.voice_mesh import consume_voice_provenance


VOICE_MODE_BY_TEMPLATE = {
    "why_reframe": "hybrid",
    "inner_dialogue": "voice_led",
    "micro_story": "voice_led",
    "quote_reflection": "hybrid",
}


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
    if speed > 1.20:
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
        command = [
            "ffmpeg", "-y", "-i", str(final_path), "-i", str(voice_path),
            "-filter_complex", "[1:a:0]loudnorm=I=-16:TP=-1.5:LRA=11[aout]",
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", str(output),
        ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Short Voice V2 did not produce a usable final master")
    return output


def _refresh_quality_final(root: Path, final_path: Path) -> dict[str, Any]:
    """Re-measure the exact voiced Short bytes that Final Critic and Gold will inspect."""
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
        "quality_measurement_stage": "post_short_voice_pre_gold",
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
    """Generate template-driven voice for standalone and source-derived Shorts before authoritative Final Master QC."""
    root = Path(output_dir)
    scope = str(control_request.get("approval_scope") or "").strip()
    if scope not in {"short_only", "short_sibling"}:
        return pre_gold

    template = str(pre_gold.get("short_template") or "").strip()
    mode = decide_voice_mode(template)
    events = list(pre_gold.get("timed_text_events") or [])
    transcript = _voice_script(events, mode)
    final_path = root / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Short Voice V2 requires final.mp4")

    gemini = secret("GEMINI_API_KEY")
    if not gemini:
        raise RuntimeError("Short Voice V2 requires Gemini key for Voice Mesh primary")
    model = env("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview") or "gemini-3.1-flash-tts-preview"
    voice = env("GEMINI_TTS_VOICE", "Gacrux") or "Gacrux"
    voice_path = root / "short-voice-v2.wav"
    orchestrator._synthesize_tts_section(
        ledger,
        TtsCircuit(),
        TtsBudget(max_extra_attempts=1),
        task_id="SHORT_VOICE_V2",
        api_key=gemini,
        transcript=transcript,
        output=voice_path,
        model=model,
        voice=voice,
        style=(
            "Warm, mature, natural Modern Standard Arabic. Calm confidence; no announcer tone. "
            f"Short template: {template}; delivery mode: {mode}."
        ),
    )
    provenance = consume_voice_provenance(voice_path)
    fitted_voice = _fit_voice_to_video(voice_path, _final_duration(final_path))
    voiced = root / "final-short-voice-v2.mp4"
    _mix_voice(final_path, fitted_voice, voiced)
    shutil.move(str(voiced), str(final_path))

    provider = str(provenance.get("provider") or "unknown")
    fallback_used = provenance.get("fallback_used")
    quality = _refresh_quality_final(root, final_path)
    _record_voice_rights(
        root,
        provider=provider,
        fallback_used=fallback_used,
        model=model,
        voice=voice,
    )

    updated = dict(pre_gold)
    compensation = dict(updated.get("compensation") or {})
    compensation.update(
        {
            "voice_generated": True,
            "voice_mode": mode,
            "voice_template_driven": True,
            "voice_scope": scope,
            "voice_provider": provider,
            "voice_fallback_used": fallback_used,
            "voice_task_id": "SHORT_VOICE_V2",
            "gemini_provider_attempt_cap": 1,
            "piper_local_fallback": True,
            "extra_text_ai_calls": 0,
            "extra_ai_calls": 1,
            "existing_audio_preserved": True,
            "quality_final_refreshed_after_voice": True,
        }
    )
    updated["compensation"] = compensation
    updated["voice"] = {
        "mode": mode,
        "template": template,
        "scope": scope,
        "source_derived_from_long": scope == "short_sibling",
        "transcript": transcript,
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
