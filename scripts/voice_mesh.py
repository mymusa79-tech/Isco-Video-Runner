from __future__ import annotations

import math
import os
import wave
from array import array
from pathlib import Path

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.media.ffmpeg import duration
from isco_video_agent.providers.gemini import synthesize_wav as gemini_synthesize

_piper = None
_gemini_open = False


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


def synthesize(api_key: str, transcript: str, output: Path, *, model: str, voice: str, style: str = "") -> Path:
    global _gemini_open
    if not _gemini_open:
        try:
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
    _local(transcript, output)
    _qa(output, transcript)
    print("Voice provider selected: piper-local")
    return output


def install_voice_mesh() -> None:
    orchestrator.synthesize_wav = synthesize
    print("Voice Mesh installed: Gemini -> Piper Local -> QA")
