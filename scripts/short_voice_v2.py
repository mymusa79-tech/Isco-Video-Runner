from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import BudgetLedger
from isco_video_agent.config import env, secret
from isco_video_agent.media.ffmpeg import duration, probe
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


def apply_short_voice_v2(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
    *,
    ledger: BudgetLedger,
) -> dict[str, Any]:
    """Generate type-driven standalone Short voice before authoritative Final Master QC."""
    root = Path(output_dir)
    if str(control_request.get("approval_scope") or "").strip() != "short_only":
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

    updated = dict(pre_gold)
    compensation = dict(updated.get("compensation") or {})
    provider = str(provenance.get("provider") or "unknown")
    fallback_used = provenance.get("fallback_used")
    compensation.update(
        {
            "voice_generated": True,
            "voice_mode": mode,
            "voice_template_driven": True,
            "voice_provider": provider,
            "voice_fallback_used": fallback_used,
            "voice_task_id": "SHORT_VOICE_V2",
            "tts_provider_attempt_cap": 1,
            "piper_local_fallback": True,
            "extra_text_ai_calls": 0,
            "extra_ai_calls": 1 if provider.startswith("gemini") or fallback_used is True else 0,
            "existing_audio_preserved": True,
        }
    )
    updated["compensation"] = compensation
    updated["voice"] = {
        "mode": mode,
        "template": template,
        "transcript": transcript,
        "provider": provider,
        "fallback_used": fallback_used,
        "generated_before_authoritative_final_master_qc": True,
    }
    (root / "short-intelligence-pre-gold.json").write_text(
        json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return updated
