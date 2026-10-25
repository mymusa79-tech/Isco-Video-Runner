from __future__ import annotations

import math
import os
import re
import wave
from array import array
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.audio_pacing import add_tail_silence_in_place, section_tail_seconds
from isco_video_agent.media.ffmpeg import duration

from clean_v2.media import (
    GEMINI38_PRIMARY_VOICE,
    GEMINI38_QUESTIONER_VOICE,
    GEMINI38_TTS_MODEL,
    _gemini38_synthesize,
)

_voice_provenance: dict[str, dict] = {}

# Compatibility seam for the legacy V4 orchestrator. The provider contract is now
# identical to Clean V2: Gemini 3.8 only, fixed Charon/Orus roster, fail closed.
DIALOGUE_QUESTIONER_VOICE = GEMINI38_QUESTIONER_VOICE
DIALOGUE_RESPONDER_VOICE = GEMINI38_PRIMARY_VOICE
_DIALOGUE_LABEL = re.compile(r"(?m)^\s*(السائل|المجيب)\s*:\s*")

VOICE_QA_RMS_FLOOR = 25.0
VOICE_QA_MAX_CONSECUTIVE_SILENT_WINDOWS = 5


def _output_key(path: Path) -> str:
    try:
        return str(path.resolve())
    except Exception:
        return str(path)


def _record_voice_provenance(output: Path, *, provider: str, fallback_used: bool) -> None:
    _voice_provenance[_output_key(output)] = {
        "provider": provider,
        "fallback_used": fallback_used,
    }


def record_voice_provenance(output: Path, *, provider: str, fallback_used: bool) -> None:
    _record_voice_provenance(Path(output), provider=provider, fallback_used=fallback_used)


def peek_voice_provenance(output: Path) -> dict:
    return dict(
        _voice_provenance.get(
            _output_key(Path(output)),
            {"provider": "unknown", "fallback_used": None},
        )
    )


def consume_voice_provenance(output: Path) -> dict:
    return _voice_provenance.pop(
        _output_key(output),
        {"provider": "unknown", "fallback_used": None},
    )


def _qa(path: Path, text: str) -> None:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("voice_qa_empty")

    total_square = 0.0
    total_samples = 0
    consecutive_silent_windows = 0
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        if rate < 16000 or width != 2:
            raise RuntimeError("voice_qa_format")
        while True:
            raw = w.readframes(rate)
            if not raw:
                break
            samples = array("h")
            samples.frombytes(raw)
            if not samples:
                continue
            square_sum = sum(float(x) * float(x) for x in samples)
            sample_count = len(samples)
            total_square += square_sum
            total_samples += sample_count
            window_rms = math.sqrt(square_sum / sample_count)
            if window_rms < VOICE_QA_RMS_FLOOR:
                consecutive_silent_windows += 1
                if consecutive_silent_windows >= VOICE_QA_MAX_CONSECUTIVE_SILENT_WINDOWS:
                    raise RuntimeError("voice_qa_silence")
            else:
                consecutive_silent_windows = 0

    seconds = float(duration(path))
    words = max(1, len(text.split()))
    if seconds < max(1.0, words * 0.12) or seconds > max(8.0, words * 1.35):
        raise RuntimeError("voice_qa_duration")
    if total_samples <= 0:
        raise RuntimeError("voice_qa_samples")
    if math.sqrt(total_square / total_samples) < VOICE_QA_RMS_FLOOR:
        raise RuntimeError("voice_qa_silence")


def qa_voice_output(path: Path, transcript: str) -> None:
    text = _DIALOGUE_LABEL.sub("", transcript) if os.environ.get("ISCO_DIALOGUE_QA") == "1" else transcript
    _qa(Path(path), text)


def _legacy_dialogue_to_ab(transcript: str) -> str:
    """Translate the old Arabic labels to the fixed Gemini 3.8 A/B metadata seam."""
    source = str(transcript or "")
    source = re.sub(r"(?m)^\s*السائل\s*:\s*", "A: ", source)
    source = re.sub(r"(?m)^\s*المجيب\s*:\s*", "B: ", source)
    return source.strip()


def synthesize(
    api_key: str,
    transcript: str,
    output: Path,
    *,
    model: str,
    voice: str,
    style: str = "",
    attempts: int = 1,
) -> Path:
    del voice, style
    if attempts != 1:
        raise RuntimeError(
            f"voice_mesh_retry_owner_violation attempts={attempts} expected=1"
        )
    if str(model or "") != GEMINI38_TTS_MODEL:
        raise RuntimeError(
            f"voice_mesh_model_drift expected={GEMINI38_TTS_MODEL} actual={model}"
        )

    dialogue = os.environ.get("ISCO_DIALOGUE_QA") == "1"
    spoken = _legacy_dialogue_to_ab(transcript) if dialogue else str(transcript or "").strip()
    if not spoken:
        raise RuntimeError("voice_mesh_empty_transcript")

    _gemini38_synthesize(
        api_key,
        spoken,
        output,
        model=GEMINI38_TTS_MODEL,
        primary_voice=DIALOGUE_RESPONDER_VOICE,
        questioner_voice=DIALOGUE_QUESTIONER_VOICE,
    )
    if dialogue:
        add_tail_silence_in_place(output, section_tail_seconds(transcript))
        _qa(output, _DIALOGUE_LABEL.sub("", transcript))
        provider = "gemini-3.8:Charon+Orus"
    else:
        _qa(output, transcript)
        provider = "gemini-3.8:Charon"
    _record_voice_provenance(output, provider=provider, fallback_used=False)
    print(f"Voice provider selected: {provider} fallback=false")
    return output


def synthesize_local_wav(transcript: str, output: Path) -> Path:
    del transcript, output
    raise RuntimeError(
        "voice_local_fallback_retired: Gemini 3.8 is the only approved production voice"
    )


def install_voice_mesh() -> None:
    # Keep the legacy Engine seam callable without retaining a second provider family.
    # If the Engine asks for its historical local fallback, synthesize_local_wav fails
    # closed instead of changing narrator/provider.
    orchestrator.synthesize_wav = synthesize
    orchestrator.synthesize_local_wav = synthesize_local_wav
    from scripts.provider_retry_ownership import certify_provider_retry_ownership

    certify_provider_retry_ownership()
    print(
        "Voice Mesh compatibility seam installed: Gemini 3.8 only; "
        f"voices={DIALOGUE_QUESTIONER_VOICE}/{DIALOGUE_RESPONDER_VOICE}; "
        "local_fallback=retired provider_attempts=1"
    )
