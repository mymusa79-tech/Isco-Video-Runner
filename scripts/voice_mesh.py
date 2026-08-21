from __future__ import annotations

import base64
import math
import os
import re
import wave
from array import array
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.audio_pacing import add_tail_silence_in_place, section_tail_seconds
from isco_video_agent.media.ffmpeg import concat_audio, duration
from isco_video_agent.providers.gemini import synthesize_wav as gemini_synthesize

_piper = None
_voice_provenance: dict[str, dict] = {}

# Human-approved Voice Roster V1. Fixed, never randomized per video.
DIALOGUE_QUESTIONER_VOICE = "Orus"
DIALOGUE_RESPONDER_VOICE = "Charon"
_DIALOGUE_LABEL = re.compile(r"(?m)^\s*(السائل|المجيب)\s*:\s*")

# Local fallback must stay bounded on low-memory runners. Long Film sections are split
# at natural Arabic/Latin sentence boundaries before Piper, then concatenated locally.
# This adds no provider calls and does not change cloud retry ownership.
PIPER_MAX_CHARS_PER_CHUNK = 420
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?؟!؛])\s+|\n+")

# Acoustic QA scans the whole synthesized WAV in bounded one-second windows. The
# historical first-15s sample could miss a truncated/silent tail after Piper chunk
# concatenation. Keep the existing RMS floor and allow ordinary rhetorical pauses,
# while failing closed on a sustained five-second acoustic dropout anywhere in-file.
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
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        if rate < 16000 or width != 2:
            raise RuntimeError("voice_qa_format")

        # Stream the complete WAV rather than materializing long Film audio in memory.
        # One-second windows are only for dropout detection; the overall RMS below is
        # still computed across every PCM sample in the file.
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
    rms = math.sqrt(total_square / total_samples)
    if rms < VOICE_QA_RMS_FLOOR:
        raise RuntimeError("voice_qa_silence")


def _local_voice():
    global _piper
    if _piper is None:
        from piper import PiperVoice
        model = Path(os.environ["PIPER_MODEL_PATH"])
        _piper = PiperVoice.load(str(model), config_path=str(model) + ".json")
    return _piper


def _split_long_piece(piece: str, max_chars: int) -> list[str]:
    words = piece.split()
    if not words:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        added = len(word) if not current else len(word) + 1
        if current and current_len + added > max_chars:
            chunks.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        chunks.append(" ".join(current))
    return chunks


def _piper_chunks(text: str, max_chars: int = PIPER_MAX_CHARS_PER_CHUNK) -> list[str]:
    """Deterministic natural-boundary chunks for the local Piper fallback.

    Prefer sentence boundaries. If one sentence itself exceeds the limit, fall back to
    word-boundary splitting. No text is dropped or paraphrased; whitespace is merely
    normalized between words/chunks.
    """
    normalized = str(text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    current = ""
    for piece in [x.strip() for x in _SENTENCE_BOUNDARY.split(normalized) if x.strip()]:
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_piece(piece, max_chars))
            continue
        candidate = piece if not current else current + " " + piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _synthesize_piper_piece(text: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        _local_voice().synthesize_wav(text, wav)
    return output


def _local(text: str, output: Path) -> Path:
    chunks = _piper_chunks(text)
    if not chunks:
        raise RuntimeError("voice_local_empty_transcript")
    if len(chunks) == 1:
        return _synthesize_piper_piece(chunks[0], output)

    parts: list[Path] = []
    try:
        for index, chunk in enumerate(chunks, 1):
            part = output.with_name(f"{output.stem}-piper-chunk-{index:02d}.wav")
            _synthesize_piper_piece(chunk, part)
            parts.append(part)
        concat_audio(parts, output)
    finally:
        for part in parts:
            part.unlink(missing_ok=True)
    print(f"Piper local fallback chunked safely: chunks={len(chunks)}")
    return output


def _dialogue_turns(transcript: str) -> list[tuple[str, str]]:
    matches = list(_DIALOGUE_LABEL.finditer(transcript))
    turns: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(transcript)
        text = transcript[match.end():end].strip()
        if text:
            turns.append((match.group(1), text))
    roles = {role for role, _ in turns}
    if len(turns) < 2 or roles != {"السائل", "المجيب"}:
        raise RuntimeError("dialogue_voice_contract_invalid")
    return turns


def _single_attempt_gemini_client(api_key: str):
    """Create a Gemini client with SDK retries disabled.

    P0-2 retry ownership: the Engine ledger/TTS budget owns retry/fallback decisions.
    One call through this client must therefore represent exactly one provider attempt,
    including the dialogue path that bypasses Engine providers.gemini._client().
    """
    from google import genai
    from google.genai import types as genai_types

    return genai.Client(
        api_key=api_key,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(attempts=1),
        ),
    )


def _gemini_dialogue(api_key: str, transcript: str, output: Path, *, model: str, style: str) -> Path:
    turns = _dialogue_turns(transcript)
    spoken = "\n".join(f"{role}: {text}" for role, text in turns)
    prompt = (
        "Synthesize this Arabic two-person conversation exactly as written. Do not read these directions aloud. "
        "Use clear contemporary Modern Standard Arabic. The questioner is curious, concise and grounded; the responder "
        "is the channel's warm, mature, thoughtful main voice. Keep the exchange natural, intelligent and understated. "
        "Do not add, remove, paraphrase, sing, or exaggerate. Do not race through sentence endings. "
        "Let completed thoughts breathe, give rhetorical questions a perceptible natural pause when earned, and never make all pauses identical. "
        + style
        + "\n\n### TRANSCRIPT\n"
        + spoken
    )
    client = _single_attempt_gemini_client(api_key)
    interaction = client.interactions.create(
        model=model,
        input=prompt,
        response_format={"type": "audio"},
        generation_config={
            "speech_config": [
                {"speaker": "السائل", "voice": DIALOGUE_QUESTIONER_VOICE},
                {"speaker": "المجيب", "voice": DIALOGUE_RESPONDER_VOICE},
            ]
        },
    )
    pcm = base64.b64decode(interaction.output_audio.data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm)
    return output


def _local_dialogue(transcript: str, output: Path) -> Path:
    """Fail-soft local fallback: preserve turns but keep Piper's single licensed Arabic speaker."""
    turns = _dialogue_turns(transcript)
    parts: list[Path] = []
    for index, (_, text) in enumerate(turns, 1):
        part = output.with_name(f"{output.stem}-turn-{index:02d}.wav")
        _local(text, part)
        parts.append(part)
    concat_audio(parts, output)
    for part in parts:
        part.unlink(missing_ok=True)
    print("Dialogue fallback degraded safely: Piper Arabic has one speaker; turn separation preserved")
    return output


def synthesize(
    api_key: str,
    transcript: str,
    output: Path,
    *,
    model: str,
    voice: str,
    style: str = "",
    attempts: int = 3,
) -> Path:
    dialogue = os.environ.get("ISCO_DIALOGUE_QA") == "1"
    if dialogue:
        _gemini_dialogue(api_key, transcript, output, model=model, style=style)
        add_tail_silence_in_place(output, section_tail_seconds(transcript))
        _qa(output, _DIALOGUE_LABEL.sub("", transcript))
        _record_voice_provenance(output, provider="gemini-multispeaker", fallback_used=False)
        print(
            "Voice provider selected: gemini-multispeaker "
            f"(questioner={DIALOGUE_QUESTIONER_VOICE} responder={DIALOGUE_RESPONDER_VOICE})"
        )
    else:
        # Engine gemini_synthesize() owns the cinematic pacing tail for this path.
        gemini_synthesize(
            api_key,
            transcript,
            output,
            model=model,
            voice=voice,
            style=style,
            attempts=attempts,
        )
        _qa(output, transcript)
        _record_voice_provenance(output, provider="gemini", fallback_used=False)
        print("Voice provider selected: gemini")
    return output


def synthesize_local_wav(transcript: str, output: Path) -> Path:
    dialogue = os.environ.get("ISCO_DIALOGUE_QA") == "1"
    if dialogue:
        _local_dialogue(transcript, output)
        add_tail_silence_in_place(output, section_tail_seconds(transcript))
        _qa(output, _DIALOGUE_LABEL.sub("", transcript))
        _record_voice_provenance(output, provider="piper-local-dialogue-single-speaker", fallback_used=True)
        print("Voice provider selected: piper-local-dialogue-single-speaker")
    else:
        _local(transcript, output)
        add_tail_silence_in_place(output, section_tail_seconds(transcript))
        _qa(output, transcript)
        _record_voice_provenance(output, provider="piper-local", fallback_used=True)
        print("Voice provider selected: piper-local")
    return output


def install_voice_mesh() -> None:
    orchestrator.synthesize_wav = synthesize
    orchestrator.synthesize_local_wav = synthesize_local_wav
    print(
        "Voice Mesh installed: Gemini -> Piper Local -> QA; fixed dialogue voices "
        f"{DIALOGUE_QUESTIONER_VOICE}/{DIALOGUE_RESPONDER_VOICE}"
    )

# Production trigger only: Agent pin afa2f08416ac2c0f85edb1b73f1ed17518990a93
