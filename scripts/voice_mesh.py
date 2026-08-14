from __future__ import annotations

import base64
import math
import os
import re
import wave
from array import array
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.ffmpeg import concat_audio, duration
from isco_video_agent.providers.gemini import synthesize_wav as gemini_synthesize

_piper = None
_gemini_open = False

# Channel voice identity is deliberately fixed, not randomized per video.
DIALOGUE_QUESTIONER_VOICE = "Iapetus"
DIALOGUE_RESPONDER_VOICE = "Gacrux"
_DIALOGUE_LABEL = re.compile(r"(?m)^\s*(السائل|المجيب)\s*:\s*")


def _qa(path: Path, text: str) -> None:
    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("voice_qa_empty")
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        width = w.getsampwidth()
        frames = w.getnframes()
        raw = w.readframes(min(frames, rate * 15))
    if rate < 16000 or width != 2:
        raise RuntimeError("voice_qa_format")
    seconds = float(duration(path))
    words = max(1, len(text.split()))
    if seconds < max(1.0, words * 0.12) or seconds > max(8.0, words * 1.35):
        raise RuntimeError("voice_qa_duration")
    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        raise RuntimeError("voice_qa_samples")
    rms = math.sqrt(sum(float(x) * float(x) for x in samples) / len(samples))
    if rms < 25:
        raise RuntimeError("voice_qa_silence")


def _local_voice():
    global _piper
    if _piper is None:
        from piper import PiperVoice
        model = Path(os.environ["PIPER_MODEL_PATH"])
        _piper = PiperVoice.load(str(model), config_path=str(model) + ".json")
    return _piper


def _local(text: str, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        _local_voice().synthesize_wav(text, wav)
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


def _gemini_dialogue(api_key: str, transcript: str, output: Path, *, model: str, style: str) -> Path:
    from google import genai

    turns = _dialogue_turns(transcript)
    spoken = "\n".join(f"{role}: {text}" for role, text in turns)
    prompt = (
        "Synthesize this Arabic two-person conversation exactly as written. Do not read these directions aloud. "
        "Use clear contemporary Modern Standard Arabic. The questioner is curious, concise and grounded; the responder "
        "is the channel's warm, mature, thoughtful main voice. Keep the exchange natural, intelligent and understated. "
        "Do not add, remove, paraphrase, sing, or exaggerate. Respect punctuation and short conversational pauses. "
        + style
        + "\n\n### TRANSCRIPT\n"
        + spoken
    )
    client = genai.Client(api_key=api_key)
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


def synthesize(api_key: str, transcript: str, output: Path, *, model: str, voice: str, style: str = "") -> Path:
    global _gemini_open
    dialogue = os.environ.get("ISCO_DIALOGUE_QA") == "1"
    if not _gemini_open:
        try:
            if dialogue:
                _gemini_dialogue(api_key, transcript, output, model=model, style=style)
                _qa(output, _DIALOGUE_LABEL.sub("", transcript))
                print("Voice provider selected: gemini-multispeaker (questioner=Iapetus responder=Gacrux)")
            else:
                gemini_synthesize(api_key, transcript, output, model=model, voice=voice, style=style)
                _qa(output, transcript)
                print("Voice provider selected: gemini")
            return output
        except Exception as exc:
            output.unlink(missing_ok=True)
            msg = str(exc).lower()
            if "429" in msg or "quota" in msg or "rate" in msg:
                _gemini_open = True
                print("Voice provider circuit-open: gemini")
            else:
                print("Voice provider failed safely: gemini")
    if dialogue:
        _local_dialogue(transcript, output)
        _qa(output, _DIALOGUE_LABEL.sub("", transcript))
        print("Voice provider selected: piper-local-dialogue-single-speaker")
    else:
        _local(transcript, output)
        _qa(output, transcript)
        print("Voice provider selected: piper-local")
    return output


def install_voice_mesh() -> None:
    orchestrator.synthesize_wav = synthesize
    print("Voice Mesh installed: Gemini -> Piper Local -> QA; fixed dialogue voices Iapetus/Gacrux")
