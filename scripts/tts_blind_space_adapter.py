from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import soundfile as sf
from gradio_client import Client, handle_file


CHATTERBOX_ARABIC_REFERENCE = (
    "https://storage.googleapis.com/chatterbox-demo-samples/mtl_prompts/ar_f/ar_prompts2.flac"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _write_pcm_wav(source: str, output: Path) -> None:
    audio, rate = sf.read(str(source), always_2d=False)
    if getattr(audio, "ndim", 1) > 1:
        audio = np.mean(audio, axis=1)
    output.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output), audio, int(rate), subtype="PCM_16", format="WAV")


def _voxcpm2(text: str, direction: str, output: Path) -> None:
    client = Client("openbmb/VoxCPM-Demo", verbose=False)
    source = client.predict(
        text,
        direction,
        None,
        False,
        "",
        2.0,
        False,
        False,
        api_name="/generate",
    )
    _write_pcm_wav(str(source), output)


def _chatterbox(text: str, output: Path) -> None:
    client = Client("ResembleAI/Chatterbox-Multilingual-TTS-V3", verbose=False)
    source = client.predict(
        text,
        handle_file(CHATTERBOX_ARABIC_REFERENCE),
        "ar",
        0.5,
        0.8,
        42,
        0.5,
        api_name="/generate_tts_audio",
    )
    _write_pcm_wav(str(source), output)


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
    if args.engine == "voxcpm2":
        _voxcpm2(text, direction, args.output_wav)
    else:
        _chatterbox(text, args.output_wav)

    info = sf.info(str(args.output_wav))
    if info.duration <= 0.2 or args.output_wav.stat().st_size < 512:
        raise SystemExit(f"invalid output for {args.sample_id}/{args.engine}")
    print(
        f"generated {args.sample_id}/{args.engine}: "
        f"{info.duration:.3f}s {info.samplerate}Hz {info.channels}ch"
    )


if __name__ == "__main__":
    main()
