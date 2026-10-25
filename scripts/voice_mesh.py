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
    GEMINI38_PROVIDER,
    GEMINI38_QUESTIONER_VOICE,
    GEMINI38_TTS_MODEL,
    _gemini38_synthesize,
)

_voice_provenance: dict[str, dict] = {}

# Human-approved fixed roster. Never randomized per production.
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
    """Record trusted TTS provenance for downstream observers and durable-cache hits."""
    _record_voice_provenance(Path(output), provider=provider, fallback_used=fallback_used)


def peek_voice_provenance(output: Path) -> dict:
    """Return current provenance without consuming it."""
    return dict(
        _voice_provenance.get(
            _output_key(Path(output)),
            {"provider": "unknown", "fallback_used": None},
        )
    )


def consume_voice_provenance(output: Path) -> dict:
    """Return actual final TTS provenance once, for the post-synthesis observer."""
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
    with wave.open(str(path), "rb") as wav:
        rate = wav.getframerate()
        width = wav.getsampwidth()
        if rate < 16000 or width != 2:
            raise RuntimeError("voice_qa_format")

        while True:
            raw = wav.readframes(rate)
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
    rms = math.sqrt(total_square / total_samples)
    if rms < VOICE_QA_RMS_FLOOR:
        raise RuntimeError("voice_qa_silence")


def qa_voice_output(path: Path, transcript: str) -> None:
    """Run the same final acoustic QA used by live Voice Mesh on a restored WAV."""
    text = (
        _DIALOGUE_LABEL.sub("", transcript)
        if os.environ.get("ISCO_DIALOGUE_QA") == "1"
        else transcript
    )
    _qa(Path(path), text)


def _dialogue_turns(transcript: str) -> list[tuple[str, str]]:
    matches = list(_DIALOGUE_LABEL.finditer(transcript))
    turns: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(transcript)
        spoken = transcript[match.end():end].strip()
        if spoken:
            turns.append((match.group(1), spoken))
    roles = {role for role, _ in turns}
    if len(turns) < 2 or roles != {"السائل", "المجيب"}:
        raise RuntimeError("dialogue_voice_contract_invalid")
    return turns


def _gemini38_transcript(transcript: str, *, dialogue: bool) -> str:
    if not dialogue:
        return str(transcript or "").strip()
    role_map = {"السائل": "A", "المجيب": "B"}
    return "\n".join(
        f"{role_map[role]}: {spoken}"
        for role, spoken in _dialogue_turns(transcript)
    )


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
    """Gemini 3.8-only V4 compatibility boundary.

    Engine still owns the outer bounded retry budget. This boundary performs exactly
    one Gemini 3.8 call per invocation and never substitutes a second TTS provider.
    The legacy voice/style arguments are accepted for interface compatibility but the
    user-approved Charon/Orus roster is fixed here.
    """
    del voice, style
    if attempts != 1:
        raise RuntimeError(
            f"voice_mesh_retry_owner_violation attempts={attempts} expected=1"
        )
    if str(model or "").strip() != GEMINI38_TTS_MODEL:
        raise RuntimeError(
            "voice_mesh_model_drift "
            f"expected={GEMINI38_TTS_MODEL} actual={str(model or '').strip()}"
        )

    dialogue = os.environ.get("ISCO_DIALOGUE_QA") == "1"
    spoken = _gemini38_transcript(transcript, dialogue=dialogue)
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
    add_tail_silence_in_place(output, section_tail_seconds(transcript))
    qa_text = _DIALOGUE_LABEL.sub("", transcript) if dialogue else transcript
    _qa(output, qa_text)

    provider = (
        f"{GEMINI38_PROVIDER}+{DIALOGUE_QUESTIONER_VOICE}"
        if dialogue
        else GEMINI38_PROVIDER
    )
    _record_voice_provenance(output, provider=provider, fallback_used=False)
    if dialogue:
        print(
            "Voice provider selected: Gemini 3.8 dialogue "
            f"(questioner={DIALOGUE_QUESTIONER_VOICE} responder={DIALOGUE_RESPONDER_VOICE})"
        )
    else:
        print(f"Voice provider selected: {GEMINI38_PROVIDER}")
    return output


def synthesize_local_wav(transcript: str, output: Path) -> Path:
    """Retired local-provider seam retained only for Engine interface compatibility."""
    del transcript, output
    raise RuntimeError("voice_local_fallback_retired_gemini_only_fail_closed")


def install_voice_mesh() -> None:
    """Install Gemini 3.8 as the only V4 TTS provider.

    The Engine's local seam remains patched to a terminal fail-closed function so an
    exhausted Gemini failure can never silently change the narrator or provider.
    """
    orchestrator.synthesize_wav = synthesize
    orchestrator.synthesize_local_wav = synthesize_local_wav

    from scripts.provider_retry_ownership import certify_provider_retry_ownership

    certify_provider_retry_ownership()
    print(
        "Voice Mesh installed: Gemini 3.8 only -> QA -> fail closed; fixed voices "
        f"{DIALOGUE_QUESTIONER_VOICE}/{DIALOGUE_RESPONDER_VOICE}; provider_attempts=1"
    )
