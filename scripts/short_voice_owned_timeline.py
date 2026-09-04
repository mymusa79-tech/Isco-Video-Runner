from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import env, secret
from isco_video_agent.media.ffmpeg import duration
from isco_video_agent.tts_budget import TtsBudget, TtsCircuit

from scripts.short_cinematic_director import apply_short_sfx, upgrade_short_cinematic
from scripts.short_voice_v2 import (
    _final_duration,
    _has_audio,
    _record_voice_rights,
    _refresh_quality_final,
    decide_voice_mode,
)
from scripts.voice_mesh import consume_voice_provenance
from scripts.voice_owned_timeline import (
    CONTRACT_ID,
    VoiceOwnedTimelineError,
    build_voice_owned_timeline,
    retime_events,
)


# Keep the historical task identity so Voice Mesh/TTS cache/observability continuity
# is preserved while the timeline ownership contract changes underneath it.
VOICE_TASK_ID = "SHORT_VOICE_V2"

_PERFORMANCE_STYLE_BY_TEMPLATE = {
    "why_reframe": (
        "Natural Modern Standard Arabic, intimate and human. Begin with quiet curiosity and a trace of doubt; "
        "let the realization arrive rather than announcing it; finish with calm clarity. Use meaningful micro-pauses. "
        "No announcer cadence, no motivational shouting, no melodrama."
    ),
    "inner_dialogue": (
        "Natural Modern Standard Arabic, close and inward. Allow slight hesitation and restrained anxiety where the words "
        "carry uncertainty, then gradually settle into steadier breathing and grounded calm. Preserve human pauses and "
        "sentence endings. Never rush, perform theatrically, or sound like an announcer."
    ),
    "micro_story": (
        "Natural Modern Standard Arabic, conversational storytelling. Start observationally, allow subtle tension to build, "
        "mark the turning point with a small pause, then release into a warm grounded payoff. Human pacing; no trailer voice."
    ),
    "quote_reflection": (
        "Natural Modern Standard Arabic, quiet and reflective. Give the key line room to land, use one meaningful pause before "
        "the reflection, and end personally rather than grandly. Warm, restrained, non-melodramatic delivery."
    ),
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _performance_script(events: list[dict[str, Any]], mode: str, template: str) -> str:
    """Preserve approved words while giving Gemini sentence/beat breathing cues.

    This is punctuation-only performance shaping: no beat is rewritten, summarized or
    invented. Voice-led templates speak every approved beat; hybrid templates speak the
    hook and payoff while the intermediate beats remain visual/on-screen information.
    """
    texts = [_clean(item.get("text")) for item in events if isinstance(item, dict) and _clean(item.get("text"))]
    if len(texts) < 2:
        raise RuntimeError("Voice-Owned Timeline requires at least two semantic beats")
    if mode == "hybrid":
        texts = [texts[0], texts[-1]]
    elif mode != "voice_led":
        raise RuntimeError("Voice-Owned Timeline received unsupported voice mode")

    cleaned = [text.rstrip(" .،!?؟…") for text in texts]
    if template in {"inner_dialogue", "quote_reflection"}:
        separator = "… "
    elif template == "micro_story":
        separator = ". "
    elif template == "why_reframe":
        separator = "… " if len(cleaned) <= 2 else ". "
    else:
        raise RuntimeError("Voice-Owned Timeline received unsupported Short template")
    return separator.join(cleaned) + "."


def _read_quality(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads((root / "quality-final.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Voice-Owned Timeline requires valid quality-final.json") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Voice-Owned Timeline quality-final.json must be an object")
    return payload


def _stage_visual_duration(source: Path, output: Path, target_seconds: float) -> Path:
    source_seconds = _final_duration(source)
    target_seconds = float(target_seconds)
    if target_seconds <= 0:
        raise RuntimeError("Voice-Owned Timeline target duration is invalid")

    if abs(target_seconds - source_seconds) <= 0.03:
        shutil.copy2(source, output)
        return output

    if target_seconds < source_seconds:
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-t", f"{target_seconds:.3f}",
            "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-movflags", "+faststart", str(output),
        ]
    else:
        extra = target_seconds - source_seconds
        if _has_audio(source):
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-filter_complex",
                f"[0:v:0]tpad=stop_mode=clone:stop_duration={extra:.3f},fps=30,setsar=1,format=yuv420p[v];"
                f"[0:a:0]apad=pad_dur={extra:.3f}[a]",
                "-map", "[v]", "-map", "[a]", "-t", f"{target_seconds:.3f}",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                "-movflags", "+faststart", str(output),
            ]
        else:
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source),
                "-vf", f"tpad=stop_mode=clone:stop_duration={extra:.3f},fps=30,setsar=1,format=yuv420p",
                "-t", f"{target_seconds:.3f}", "-an",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-movflags", "+faststart", str(output),
            ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Voice-Owned Timeline failed to stage the visual duration")
    actual = _final_duration(output)
    if abs(actual - target_seconds) > 0.15:
        raise RuntimeError(
            "Voice-Owned Timeline staged visual duration mismatch: "
            f"target={target_seconds:.3f}s actual={actual:.3f}s"
        )
    return output


def _mix_natural_voice(visual: Path, voice_path: Path, output: Path, target_seconds: float) -> Path:
    """Mix the measured natural waveform exactly as generated; never time-compress it."""
    if _has_audio(visual):
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(visual), "-i", str(voice_path),
            "-filter_complex",
            "[0:a:0]volume=0.24[bed];[1:a:0]volume=1.0[voice];"
            "[bed][voice]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[aout]",
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-t", f"{target_seconds:.3f}", "-movflags", "+faststart", str(output),
        ]
    else:
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(visual), "-i", str(voice_path),
            "-filter_complex", "[1:a:0]loudnorm=I=-16:TP=-1.5:LRA=11,apad[aout]",
            "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
            "-t", f"{target_seconds:.3f}", "-movflags", "+faststart", str(output),
        ]
    subprocess.run(command, check=True, capture_output=True)
    if not output.is_file() or output.stat().st_size <= 1024:
        raise RuntimeError("Voice-Owned Timeline did not produce a usable voiced master")
    return output


def _write_provisional_timeline_quality(root: Path, target_seconds: float) -> None:
    """Expose target duration to the cinematic director; authoritative measurement happens later."""
    path = root / "quality-final.json"
    quality = _read_quality(root)
    minimum = float(quality.get("duration_expected_min") or 7.0)
    maximum = float(quality.get("duration_expected_max") or 25.0)
    quality.update(
        {
            "duration_seconds": round(target_seconds, 3),
            "video_stream_duration": round(target_seconds, 3),
            "audio_stream_duration": round(target_seconds, 3),
            "duration_ok": minimum <= target_seconds <= maximum,
            "voice_owned_timeline_provisional": True,
            "voice_owned_timeline_contract": CONTRACT_ID,
            "quality_measurement_stage": "voice_owned_timeline_provisional_before_cinematic",
        }
    )
    path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")


def _performance_style(template: str, control_request: dict[str, Any], mode: str) -> str:
    base = _PERFORMANCE_STYLE_BY_TEMPLATE.get(template)
    if not base:
        raise RuntimeError("Voice-Owned Timeline received unsupported Short template")
    source_emotion = ""
    excerpt = control_request.get("source_episode_excerpt")
    if isinstance(excerpt, dict):
        source_emotion = str(excerpt.get("source_emotion") or "").strip()
    inherited = f" Inherited source emotion: {source_emotion}; keep it subtle." if source_emotion else ""
    return f"{base} Delivery mode: {mode}.{inherited} Natural timing is authoritative; do not rush to hit a duration."


def apply_voice_owned_short(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: BudgetLedger,
) -> dict[str, Any]:
    """Synthesize natural performance first, then make the Short timeline follow it."""
    root = Path(output_dir)
    scope = str(control_request.get("approval_scope") or "").strip()
    if scope not in {"short_only", "short_sibling"}:
        return pre_gold

    template = str(pre_gold.get("short_template") or "").strip()
    mode = decide_voice_mode(template)
    events = [item for item in list(pre_gold.get("timed_text_events") or []) if isinstance(item, dict)]
    if len(events) < 2:
        raise RuntimeError("Voice-Owned Timeline requires at least two semantic beats")
    transcript = _performance_script(events, mode, template)

    final_path = root / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Voice-Owned Timeline requires final.mp4")
    source_seconds = _final_duration(final_path)
    quality_before = _read_quality(root)
    minimum = float(quality_before.get("duration_expected_min") or 7.0)
    maximum = float(quality_before.get("duration_expected_max") or 25.0)

    gemini = secret("GEMINI_API_KEY")
    if not gemini:
        raise RuntimeError("Voice-Owned Timeline requires Gemini key for Voice Mesh primary")
    model = env("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview") or "gemini-3.1-flash-tts-preview"
    voice = env("GEMINI_TTS_VOICE", "Gacrux") or "Gacrux"
    voice_path = root / "short-voice-owned-v1.wav"
    orchestrator._synthesize_tts_section(
        ledger,
        TtsCircuit(),
        TtsBudget(max_extra_attempts=1),
        task_id=VOICE_TASK_ID,
        api_key=gemini,
        transcript=transcript,
        output=voice_path,
        model=model,
        voice=voice,
        style=_performance_style(template, control_request, mode),
    )
    provenance = consume_voice_provenance(voice_path)
    measured_voice_seconds = float(duration(voice_path))

    try:
        timeline = build_voice_owned_timeline(
            voice_seconds=measured_voice_seconds,
            source_visual_seconds=source_seconds,
            minimum_seconds=minimum,
            maximum_seconds=maximum,
            mode=mode,
            visible_beat_count=len(events),
            source_derived_from_long=scope == "short_sibling",
        )
    except VoiceOwnedTimelineError as exc:
        raise RuntimeError(str(exc)) from exc

    target_seconds = float(timeline["target_seconds"])
    retimed_events = retime_events(events, source_seconds=source_seconds, target_seconds=target_seconds)
    staged = root / "voice-owned-visual-stage.mp4"
    _stage_visual_duration(final_path, staged, target_seconds)
    voiced = root / "final-voice-owned-v1.mp4"
    _mix_natural_voice(staged, voice_path, voiced, target_seconds)
    shutil.move(str(voiced), str(final_path))
    staged.unlink(missing_ok=True)

    updated = dict(pre_gold)
    updated["timed_text_events"] = retimed_events
    _write_provisional_timeline_quality(root, target_seconds)

    # Standalone Shorts can use audited additional B-roll to express the measured
    # natural performance. Source-derived Shorts never add unrelated stock here: if
    # their inherited visual budget is materially too short, the contract fails closed
    # and asks for source-safe reprovisioning instead of compressing the voice.
    if control_request.get("kind") == "short" and scope == "short_only":
        updated = upgrade_short_cinematic(root, control_request, updated, ledger=ledger)
    updated = apply_short_sfx(root, updated)

    quality = _refresh_quality_final(root, final_path)
    provider = str(provenance.get("provider") or "unknown")
    fallback_used = provenance.get("fallback_used")
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
            "voice_task_id": VOICE_TASK_ID,
            "voice_timeline_contract": CONTRACT_ID,
            "voice_timeline_owner": timeline.get("timeline_owner"),
            "voice_seconds_measured": timeline.get("voice_seconds_measured"),
            "voice_target_timeline_seconds": timeline.get("target_seconds"),
            "voice_timeline_adjustment_seconds": timeline.get("timeline_adjustment_seconds"),
            "voice_post_speed_factor": 1.0,
            "voice_time_compression": False,
            "voice_duration_estimate_is_certification": False,
            "measured_voice_is_authoritative": True,
            "performance_punctuation_preserves_words": True,
            "gemini_provider_attempt_cap": 1,
            "piper_local_fallback": True,
            "extra_text_ai_calls": 0,
            "quality_final_refreshed_after_voice": True,
            "quality_final_refreshed_after_short_finishing": True,
        }
    )
    updated["compensation"] = compensation
    updated["voice"] = {
        "contract_id": CONTRACT_ID,
        "mode": mode,
        "template": template,
        "scope": scope,
        "source_derived_from_long": scope == "short_sibling",
        "transcript": transcript,
        "provider": provider,
        "fallback_used": fallback_used,
        "timeline": timeline,
        "post_speed_factor": 1.0,
        "time_compression": False,
        "performance_punctuation_preserves_words": True,
        "generated_before_authoritative_final_master_qc": True,
        "quality_final_stage": quality.get("quality_measurement_stage"),
        "rights_provenance_recorded": True,
    }
    (root / "voice-owned-timeline.json").write_text(
        json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "short-intelligence-pre-gold.json").write_text(
        json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated
