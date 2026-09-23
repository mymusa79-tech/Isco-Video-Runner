#!/usr/bin/env python3
"""One-shot naturalness probe for the existing ar_JO-kareem-medium Piper voice.

This is deliberately isolated from Clean V2 production. It compares the current
raw/default inference against a small bounded set of inference-only variants.
No model training, no provider calls, no production wiring.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import wave
from dataclasses import dataclass, asdict
from pathlib import Path

from piper import PiperVoice

TEXT = (
    "أحيانًا لا تحتاج إلى بداية جديدة، بل تحتاج إلى خطوة صادقة تعيدك إلى طريقك. "
    "لا تنتظر أن يأتي الدافع كاملًا؛ ابدأ بما تستطيع اليوم. "
    "فالاستمرار الهادئ، حين يتكرر كل يوم، يصنع فرقًا أكبر مما تتخيل."
)

# Small, bounded search around Kareem's shipped defaults.
# The goal is not to brute-force settings; it is to learn whether inference-only
# tuning creates an audible jump before considering any larger TTS project.
VARIANTS = (
    {
        "name": "00-baseline-current",
        "length_scale": 1.0,
        "noise_scale": 0.667,
        "noise_w_scale": 0.8,
        "sentence_pause_ms": 0,
        "sentence_mode": False,
    },
    {
        "name": "01-balanced-natural",
        "length_scale": 0.98,
        "noise_scale": 0.56,
        "noise_w_scale": 0.68,
        "sentence_pause_ms": 240,
        "sentence_mode": True,
    },
    {
        "name": "02-calm-reflective",
        "length_scale": 1.04,
        "noise_scale": 0.56,
        "noise_w_scale": 0.70,
        "sentence_pause_ms": 300,
        "sentence_mode": True,
    },
    {
        "name": "03-clean-direct",
        "length_scale": 0.95,
        "noise_scale": 0.50,
        "noise_w_scale": 0.60,
        "sentence_pause_ms": 220,
        "sentence_mode": True,
    },
    {
        "name": "04-expressive",
        "length_scale": 1.00,
        "noise_scale": 0.72,
        "noise_w_scale": 0.90,
        "sentence_pause_ms": 260,
        "sentence_mode": True,
    },
    {
        "name": "05-stable-smooth",
        "length_scale": 1.02,
        "noise_scale": 0.46,
        "noise_w_scale": 0.52,
        "sentence_pause_ms": 280,
        "sentence_mode": True,
    },
)


@dataclass
class WavInfo:
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width_bytes: int
    bytes: int


def wav_info(path: Path) -> WavInfo:
    with wave.open(str(path), "rb") as wf:
        duration = wf.getnframes() / float(wf.getframerate())
        return WavInfo(
            duration_seconds=round(duration, 3),
            sample_rate=wf.getframerate(),
            channels=wf.getnchannels(),
            sample_width_bytes=wf.getsampwidth(),
            bytes=path.stat().st_size,
        )


def _sentences(text: str) -> list[str]:
    # Keep terminal punctuation attached so Piper/eSpeak still owns phrase-final prosody.
    import re

    parts = re.findall(r".+?(?:[.!؟?]+|$)", text.strip(), flags=re.S)
    return [part.strip() for part in parts if part.strip()]


def _write_silence(handle: wave.Wave_write, *, ms: int, rate: int, channels: int, width: int) -> None:
    if ms <= 0:
        return
    frames = int(rate * (ms / 1000.0))
    handle.writeframes(b"\x00" * frames * channels * width)


def _synthesize_default(voice: PiperVoice, text: str, output: Path) -> None:
    with wave.open(str(output), "wb") as wav:
        voice.synthesize_wav(text, wav)


def _synthesize_sentence_mode(
    voice: PiperVoice,
    text: str,
    output: Path,
    *,
    pause_ms: int,
) -> None:
    sentences = _sentences(text)
    if not sentences:
        raise RuntimeError("no sentences")

    with tempfile.TemporaryDirectory(prefix="piper-naturalness-") as tmp:
        sentence_paths: list[Path] = []
        for index, sentence in enumerate(sentences):
            path = Path(tmp) / f"{index:02d}.wav"
            with wave.open(str(path), "wb") as wav:
                voice.synthesize_wav(sentence, wav)
            sentence_paths.append(path)

        with wave.open(str(output), "wb") as dst:
            params = None
            for index, path in enumerate(sentence_paths):
                with wave.open(str(path), "rb") as src:
                    if params is None:
                        params = src.getparams()
                        dst.setparams(params)
                    elif (
                        src.getnchannels(),
                        src.getsampwidth(),
                        src.getframerate(),
                        src.getcomptype(),
                    ) != (
                        params.nchannels,
                        params.sampwidth,
                        params.framerate,
                        params.comptype,
                    ):
                        raise RuntimeError("sentence WAV format mismatch")
                    dst.writeframes(src.readframes(src.getnframes()))
                    if index + 1 < len(sentence_paths):
                        _write_silence(
                            dst,
                            ms=pause_ms,
                            rate=params.framerate,
                            channels=params.nchannels,
                            width=params.sampwidth,
                        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", default="probe_artifacts/piper-kareem-naturalness")
    args = parser.parse_args()

    model = Path(args.model)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    voice = PiperVoice.load(str(model), config_path=str(model) + ".json")
    baseline_config = {
        "length_scale": float(voice.config.length_scale),
        "noise_scale": float(voice.config.noise_scale),
        "noise_w_scale": float(voice.config.noise_w_scale),
        "sample_rate": int(voice.config.sample_rate),
        "espeak_voice": str(voice.config.espeak_voice),
    }

    # Current Piper supports an Arabic tashkeel stage. Record whether the installed
    # build exposes it; do not mutate the setting in this probe.
    tashkeel_available = hasattr(voice, "use_tashkeel")
    tashkeel_enabled = bool(getattr(voice, "use_tashkeel", False)) if tashkeel_available else None

    results = []
    for variant in VARIANTS:
        voice.config.length_scale = float(variant["length_scale"])
        voice.config.noise_scale = float(variant["noise_scale"])
        voice.config.noise_w_scale = float(variant["noise_w_scale"])

        path = output / f'{variant["name"]}.wav'
        started = time.perf_counter()
        if variant["sentence_mode"]:
            _synthesize_sentence_mode(
                voice,
                TEXT,
                path,
                pause_ms=int(variant["sentence_pause_ms"]),
            )
        else:
            _synthesize_default(voice, TEXT, path)
        generation_seconds = time.perf_counter() - started
        info = wav_info(path)

        results.append(
            {
                **variant,
                "generation_seconds": round(generation_seconds, 3),
                "realtime_factor": round(generation_seconds / info.duration_seconds, 3),
                "wav": asdict(info),
                "path": str(path),
            }
        )

    report = {
        "status": "success",
        "voice": "ar_JO-kareem-medium",
        "text": TEXT,
        "text_chars": len(TEXT),
        "baseline_voice_config": baseline_config,
        "tashkeel_available": tashkeel_available,
        "tashkeel_enabled": tashkeel_enabled,
        "variant_count": len(results),
        "results": results,
        "decision_rule": (
            "Listen blind if possible. Keep inference-only tuning only if at least one "
            "variant is clearly more natural than 00-baseline-current without harming "
            "Arabic pronunciation. Otherwise stop tuning Kareem."
        ),
    }
    report_path = output / "piper-kareem-naturalness-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("PIPER_NATURALNESS_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
