from __future__ import annotations

import argparse
import hashlib
import os
import signal
import time
from pathlib import Path
from typing import Callable, TypeVar

import numpy as np
import soundfile as sf
from gradio_client import Client, handle_file


CHATTERBOX_ARABIC_REFERENCE = (
    "https://storage.googleapis.com/chatterbox-demo-samples/mtl_prompts/ar_f/ar_prompts2.flac"
)
REFERENCE_PROMPT_TEXT = (
    "في بعض الأيام، لا تحتاج إلى أن تغيّر حياتك كلها. يكفي أن تتوقف لحظة، "
    "وتسمع صوتك أنت، بعيدًا عن ضجيج الآخرين. الخطوة الصغيرة التي تختارها بوعي اليوم "
    "قد تكون بداية طريق مختلف تمامًا."
)
CALL_TIMEOUT_SECONDS = 180
MAX_ATTEMPTS = 3
T = TypeVar("T")


class SpaceCallTimeout(TimeoutError):
    pass


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _write_pcm_wav(source: str, output: Path) -> None:
    audio, rate = sf.read(str(source), always_2d=False)
    if getattr(audio, "ndim", 1) > 1:
        audio = np.mean(audio, axis=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output), audio, int(rate), subtype="PCM_16", format="WAV")


def _primary_reference() -> Path | None:
    raw = (os.environ.get("ISCO_TTS_PRIMARY_REFERENCE_WAV") or "").strip()
    if not raw:
        if os.environ.get("ISCO_TTS_REQUIRE_REFERENCE") == "1":
            raise RuntimeError("primary voice reference required but ISCO_TTS_PRIMARY_REFERENCE_WAV is missing")
        return None
    path = Path(raw)
    if not path.is_file() or path.stat().st_size < 512:
        raise RuntimeError(f"invalid primary voice reference: {path}")
    info = sf.info(str(path))
    if info.duration < 2.0 or info.channels < 1:
        raise RuntimeError(f"primary voice reference is too short/invalid: {info.duration:.3f}s")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    print(
        f"reference primary=Charon file={path.name} sha256={digest} duration={info.duration:.3f}s",
        flush=True,
    )
    return path


def _alarm_handler(signum, frame) -> None:  # pragma: no cover - OS signal glue
    raise SpaceCallTimeout(f"TTS Space call exceeded {CALL_TIMEOUT_SECONDS}s")


def _bounded_call(label: str, operation: Callable[[], T]) -> T:
    last: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"{label}: attempt {attempt}/{MAX_ATTEMPTS}", flush=True)
        previous = signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(CALL_TIMEOUT_SECONDS)
        try:
            result = operation()
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            return result
        except Exception as exc:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
            last = exc
            print(f"{label}: attempt {attempt} failed: {type(exc).__name__}: {exc}", flush=True)
            if attempt < MAX_ATTEMPTS:
                time.sleep(10 * attempt)
    assert last is not None
    raise last


def _voxcpm2(text: str, direction: str, output: Path, reference: Path | None) -> None:
    def generate() -> str:
        client = Client("openbmb/VoxCPM-Demo", verbose=False)
        if reference is not None:
            prompt_text = REFERENCE_PROMPT_TEXT
            prompt_audio = handle_file(str(reference))
        else:
            prompt_text = direction
            prompt_audio = None
        return str(
            client.predict(
                text,
                prompt_text,
                prompt_audio,
                False,
                "",
                2.0,
                False,
                False,
                api_name="/generate",
            )
        )

    source = _bounded_call("voxcpm2", generate)
    _write_pcm_wav(source, output)


def _chatterbox(text: str, output: Path, reference: Path | None) -> None:
    def generate() -> str:
        client = Client("ResembleAI/Chatterbox-Multilingual-TTS-V3", verbose=False)
        prompt_audio = handle_file(str(reference)) if reference is not None else handle_file(CHATTERBOX_ARABIC_REFERENCE)
        return str(
            client.predict(
                text,
                prompt_audio,
                "ar",
                0.5,
                0.8,
                42,
                0.5,
                api_name="/generate_tts_audio",
            )
        )

    source = _bounded_call("chatterbox_multilingual_v3", generate)
    _write_pcm_wav(source, output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("engine", choices=("voxcpm2", "chatterbox_multilingual_v3"))
    parser.add_argument("text_file", type=Path)
    parser.add_argument("direction_file", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("sample_id")
    args = parser.parse_args()

    text = _read(args.text_file)
    direction = _read(args.direction_file)
    reference = _primary_reference()
    print(f"starting {args.sample_id}/{args.engine} reference={'Charon' if reference else 'engine-default'}", flush=True)
    if args.engine == "voxcpm2":
        _voxcpm2(text, direction, args.output_wav, reference)
    else:
        _chatterbox(text, args.output_wav, reference)

    info = sf.info(str(args.output_wav))
    if info.duration <= 0.2 or args.output_wav.stat().st_size < 512:
        raise SystemExit(f"invalid output for {args.sample_id}/{args.engine}")
    print(
        f"generated {args.sample_id}/{args.engine}: "
        f"{info.duration:.3f}s {info.samplerate}Hz {info.channels}ch",
        flush=True,
    )


if __name__ == "__main__":
    main()
