#!/usr/bin/env python3
"""One-shot naturalness probe for the existing ar_JO-kareem-medium Piper voice.

Isolated from Clean V2 production. First compares bounded global settings, then
adds a final user-feedback round that targets only two observed defects:
(1) heavy/muddy timbre, and (2) same prosodic cadence across sentences.

No model training, no provider calls, no production wiring.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path

from piper import PiperVoice
from piper.config import SynthesisConfig

TEXT = (
    "أحيانًا لا تحتاج إلى بداية جديدة، بل تحتاج إلى خطوة صادقة تعيدك إلى طريقك. "
    "لا تنتظر أن يأتي الدافع كاملًا؛ ابدأ بما تستطيع اليوم. "
    "فالاستمرار الهادئ، حين يتكرر كل يوم، يصنع فرقًا أكبر مما تتخيل."
)

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

# Final bounded round based on the listener's feedback:
# - speech is ~80% clear
# - timbre is heavy/uncomfortable
# - every sentence has nearly the same cadence
#
# Each profile deliberately changes pacing/noise per sentence. A very light
# deterministic EQ then removes some low-mid weight and restores presence.
DYNAMIC_VARIANTS = (
    {
        "name": "06-dynamic-light",
        "profiles": (
            {"length_scale": 0.94, "noise_scale": 0.54, "noise_w_scale": 0.72},
            {"length_scale": 1.01, "noise_scale": 0.48, "noise_w_scale": 0.58},
            {"length_scale": 1.06, "noise_scale": 0.56, "noise_w_scale": 0.74},
        ),
        "pauses_ms": (170, 310),
        "eq_filter": (
            "highpass=f=75,"
            "equalizer=f=230:t=q:w=1.15:g=-2.5,"
            "equalizer=f=2900:t=q:w=1.0:g=1.6"
        ),
    },
    {
        "name": "07-dynamic-conversational",
        "profiles": (
            {"length_scale": 0.96, "noise_scale": 0.52, "noise_w_scale": 0.68},
            {"length_scale": 1.04, "noise_scale": 0.50, "noise_w_scale": 0.60},
            {"length_scale": 0.99, "noise_scale": 0.60, "noise_w_scale": 0.80},
        ),
        "pauses_ms": (140, 260),
        "eq_filter": (
            "highpass=f=70,"
            "equalizer=f=250:t=q:w=1.2:g=-2.0,"
            "equalizer=f=3200:t=q:w=1.0:g=1.8"
        ),
    },
    {
        "name": "08-dynamic-soft",
        "profiles": (
            {"length_scale": 0.99, "noise_scale": 0.45, "noise_w_scale": 0.55},
            {"length_scale": 1.05, "noise_scale": 0.47, "noise_w_scale": 0.61},
            {"length_scale": 1.09, "noise_scale": 0.51, "noise_w_scale": 0.66},
        ),
        "pauses_ms": (220, 360),
        "eq_filter": (
            "highpass=f=70,"
            "equalizer=f=240:t=q:w=1.15:g=-3.0,"
            "equalizer=f=3000:t=q:w=1.0:g=1.3"
        ),
    },
    {
        "name": "09-soft-lighter-gentle",
        "profiles": (
            {"length_scale": 0.98, "noise_scale": 0.45, "noise_w_scale": 0.55},
            {"length_scale": 1.03, "noise_scale": 0.47, "noise_w_scale": 0.61},
            {"length_scale": 1.07, "noise_scale": 0.51, "noise_w_scale": 0.66},
        ),
        "pauses_ms": (200, 330),
        "eq_filter": (
            "highpass=f=78,"
            "equalizer=f=220:t=q:w=1.10:g=-3.8,"
            "equalizer=f=450:t=q:w=1.0:g=-1.2,"
            "equalizer=f=3200:t=q:w=1.0:g=1.7,"
            "asetrate=22381,aresample=22050,atempo=1.008"
        ),
    },
    {
        "name": "10-soft-lighter-natural",
        "profiles": (
            {"length_scale": 0.97, "noise_scale": 0.45, "noise_w_scale": 0.55},
            {"length_scale": 1.02, "noise_scale": 0.47, "noise_w_scale": 0.61},
            {"length_scale": 1.06, "noise_scale": 0.51, "noise_w_scale": 0.66},
        ),
        "pauses_ms": (190, 315),
        "eq_filter": (
            "highpass=f=82,"
            "equalizer=f=220:t=q:w=1.10:g=-4.3,"
            "equalizer=f=460:t=q:w=1.0:g=-1.5,"
            "equalizer=f=3300:t=q:w=1.0:g=2.0,"
            "asetrate=22535,aresample=22050,atempo=1.010"
        ),
    },
    {
        "name": "11-soft-lighter-bright",
        "profiles": (
            {"length_scale": 0.96, "noise_scale": 0.45, "noise_w_scale": 0.55},
            {"length_scale": 1.01, "noise_scale": 0.47, "noise_w_scale": 0.61},
            {"length_scale": 1.05, "noise_scale": 0.51, "noise_w_scale": 0.66},
        ),
        "pauses_ms": (180, 300),
        "eq_filter": (
            "highpass=f=86,"
            "equalizer=f=210:t=q:w=1.05:g=-4.8,"
            "equalizer=f=480:t=q:w=1.0:g=-1.8,"
            "equalizer=f=3400:t=q:w=1.0:g=2.2,"
            "asetrate=22712,aresample=22050,atempo=1.012"
        ),
    },
)

# Listener-selected direction after 09:
# keep the lighter/comfortable timbre, but remove the "compressed/serious"
# delivery by phrasing the same message as six semantic beats. Selective Arabic
# diacritics are used only where they help pronunciation; no artificial tatweel
# characters are inserted.
PERFORMANCE_TEXTS = (
    "أحيانًا، لا تحتاج إلى بِداية جديدة",
    "بل تحتاج إلى خُطوة صادقة، تعيدك إلى طريقك",
    "لا تنتظر أن يأتي الدافع كاملًا",
    "ابدأ بما تستطيع اليوم",
    "فالاستمرار الهادئ، حين يتكرر كل يوم",
    "يصنع فرقًا أكبر مما تتخيل",
)

PERFORMANCE_VARIANTS = (
    {
        "name": "12-warm-breathing",
        "profiles": (
            {"length_scale": 1.01, "noise_scale": 0.45, "noise_w_scale": 0.55},
            {"length_scale": 1.08, "noise_scale": 0.47, "noise_w_scale": 0.62},
            {"length_scale": 0.98, "noise_scale": 0.46, "noise_w_scale": 0.58},
            {"length_scale": 1.03, "noise_scale": 0.50, "noise_w_scale": 0.64},
            {"length_scale": 1.10, "noise_scale": 0.48, "noise_w_scale": 0.62},
            {"length_scale": 1.06, "noise_scale": 0.52, "noise_w_scale": 0.68},
        ),
        "pauses_ms": (180, 360, 210, 340, 240),
        "eq_filter": (
            "highpass=f=76,"
            "equalizer=f=220:t=q:w=1.10:g=-3.7,"
            "equalizer=f=450:t=q:w=1.0:g=-1.1,"
            "equalizer=f=3150:t=q:w=1.0:g=1.5,"
            "asetrate=22381,aresample=22050,atempo=0.996"
        ),
    },
    {
        "name": "13-gentle-reflective",
        "profiles": (
            {"length_scale": 1.03, "noise_scale": 0.44, "noise_w_scale": 0.54},
            {"length_scale": 1.11, "noise_scale": 0.46, "noise_w_scale": 0.60},
            {"length_scale": 0.99, "noise_scale": 0.45, "noise_w_scale": 0.56},
            {"length_scale": 1.05, "noise_scale": 0.49, "noise_w_scale": 0.63},
            {"length_scale": 1.13, "noise_scale": 0.47, "noise_w_scale": 0.60},
            {"length_scale": 1.08, "noise_scale": 0.50, "noise_w_scale": 0.66},
        ),
        "pauses_ms": (210, 410, 230, 390, 270),
        "eq_filter": (
            "highpass=f=74,"
            "equalizer=f=220:t=q:w=1.10:g=-3.5,"
            "equalizer=f=440:t=q:w=1.0:g=-1.0,"
            "equalizer=f=3050:t=q:w=1.0:g=1.3,"
            "asetrate=22381,aresample=22050,atempo=0.992"
        ),
    },
    {
        "name": "14-soft-conversational",
        "profiles": (
            {"length_scale": 0.99, "noise_scale": 0.48, "noise_w_scale": 0.62},
            {"length_scale": 1.06, "noise_scale": 0.50, "noise_w_scale": 0.68},
            {"length_scale": 0.96, "noise_scale": 0.49, "noise_w_scale": 0.64},
            {"length_scale": 1.00, "noise_scale": 0.54, "noise_w_scale": 0.72},
            {"length_scale": 1.08, "noise_scale": 0.50, "noise_w_scale": 0.66},
            {"length_scale": 1.03, "noise_scale": 0.56, "noise_w_scale": 0.74},
        ),
        "pauses_ms": (160, 330, 180, 320, 220),
        "eq_filter": (
            "highpass=f=78,"
            "equalizer=f=220:t=q:w=1.10:g=-3.8,"
            "equalizer=f=450:t=q:w=1.0:g=-1.2,"
            "equalizer=f=3200:t=q:w=1.0:g=1.7,"
            "asetrate=22381,aresample=22050,atempo=1.000"
        ),
    },
)


# Acoustic comparison against the user-provided target short showed three
# dominant gaps versus variant 14: much wider intonation, substantially longer
# idea-level pauses, and a finished loudness/mastering profile near -16 LUFS.
# These variants keep 14's lighter timbre but add only those three properties.
TARGET_MATCH_VARIANTS = (
    {
        "name": "15-target-comfort-balanced",
        "profiles": (
            {"length_scale": 0.99, "noise_scale": 0.48, "noise_w_scale": 0.62, "pitch_ratio": 1.032},
            {"length_scale": 1.06, "noise_scale": 0.50, "noise_w_scale": 0.68, "pitch_ratio": 1.012},
            {"length_scale": 0.97, "noise_scale": 0.49, "noise_w_scale": 0.64, "pitch_ratio": 1.045},
            {"length_scale": 1.01, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 1.025},
            {"length_scale": 1.08, "noise_scale": 0.50, "noise_w_scale": 0.66, "pitch_ratio": 1.004},
            {"length_scale": 1.04, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 0.994},
        ),
        "pauses_ms": (300, 850, 280, 760, 360),
        "eq_filter": (
            "highpass=f=76,"
            "equalizer=f=220:t=q:w=1.10:g=-3.6,"
            "equalizer=f=450:t=q:w=1.0:g=-1.0,"
            "equalizer=f=3150:t=q:w=1.0:g=1.4,"
            "acompressor=threshold=-20dB:ratio=1.6:attack=18:release=150:makeup=1.4dB,"
            "loudnorm=I=-16:LRA=3:TP=-1.5"
        ),
    },
    {
        "name": "16-target-comfort-gentle",
        "profiles": (
            {"length_scale": 1.01, "noise_scale": 0.46, "noise_w_scale": 0.58, "pitch_ratio": 1.028},
            {"length_scale": 1.08, "noise_scale": 0.48, "noise_w_scale": 0.64, "pitch_ratio": 1.010},
            {"length_scale": 0.99, "noise_scale": 0.47, "noise_w_scale": 0.60, "pitch_ratio": 1.040},
            {"length_scale": 1.03, "noise_scale": 0.51, "noise_w_scale": 0.68, "pitch_ratio": 1.022},
            {"length_scale": 1.10, "noise_scale": 0.48, "noise_w_scale": 0.62, "pitch_ratio": 1.002},
            {"length_scale": 1.06, "noise_scale": 0.52, "noise_w_scale": 0.70, "pitch_ratio": 0.992},
        ),
        "pauses_ms": (330, 930, 300, 840, 390),
        "eq_filter": (
            "highpass=f=74,"
            "equalizer=f=220:t=q:w=1.10:g=-3.4,"
            "equalizer=f=440:t=q:w=1.0:g=-0.9,"
            "equalizer=f=3050:t=q:w=1.0:g=1.2,"
            "acompressor=threshold=-20dB:ratio=1.5:attack=22:release=170:makeup=1.3dB,"
            "loudnorm=I=-16:LRA=3:TP=-1.5"
        ),
    },
    {
        "name": "17-target-comfort-expressive",
        "profiles": (
            {"length_scale": 0.98, "noise_scale": 0.50, "noise_w_scale": 0.66, "pitch_ratio": 1.040},
            {"length_scale": 1.05, "noise_scale": 0.52, "noise_w_scale": 0.72, "pitch_ratio": 1.014},
            {"length_scale": 0.96, "noise_scale": 0.51, "noise_w_scale": 0.69, "pitch_ratio": 1.052},
            {"length_scale": 1.00, "noise_scale": 0.56, "noise_w_scale": 0.76, "pitch_ratio": 1.030},
            {"length_scale": 1.07, "noise_scale": 0.52, "noise_w_scale": 0.70, "pitch_ratio": 1.000},
            {"length_scale": 1.03, "noise_scale": 0.57, "noise_w_scale": 0.76, "pitch_ratio": 0.988},
        ),
        "pauses_ms": (270, 780, 250, 710, 330),
        "eq_filter": (
            "highpass=f=78,"
            "equalizer=f=220:t=q:w=1.10:g=-3.7,"
            "equalizer=f=450:t=q:w=1.0:g=-1.1,"
            "equalizer=f=3250:t=q:w=1.0:g=1.5,"
            "acompressor=threshold=-20dB:ratio=1.7:attack=16:release=135:makeup=1.5dB,"
            "loudnorm=I=-16:LRA=3:TP=-1.5"
        ),
    },
)


# Final comfort family: preserve formants while lifting pitch, remove the
# previous 3 kHz presence boost that can become tiring, keep real idea-level
# breathing, and finish at the same 48 kHz / ~-16 LUFS delivery profile as the
# target short. Pitch values are intentionally moderate; matching the target F0
# numerically would require an unnatural jump for Kareem's voice.
COMFORT_FINAL_VARIANTS = (
    {
        "name": "18-repeat-comfort-light",
        "profiles": (
            {"length_scale": 0.99, "noise_scale": 0.48, "noise_w_scale": 0.62, "pitch_ratio": 1.07},
            {"length_scale": 1.05, "noise_scale": 0.50, "noise_w_scale": 0.68, "pitch_ratio": 1.04},
            {"length_scale": 0.97, "noise_scale": 0.49, "noise_w_scale": 0.64, "pitch_ratio": 1.09},
            {"length_scale": 1.00, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 1.07},
            {"length_scale": 1.07, "noise_scale": 0.50, "noise_w_scale": 0.66, "pitch_ratio": 1.03},
            {"length_scale": 1.03, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 1.01},
        ),
        "pauses_ms": (240, 620, 220, 560, 290),
        "eq_filter": (
            "highpass=f=68,"
            "equalizer=f=240:t=q:w=1.15:g=-2.4,"
            "equalizer=f=3300:t=q:w=1.0:g=-1.0,"
            "equalizer=f=4700:t=q:w=1.0:g=-0.6,"
            "loudnorm=I=-16:LRA=3:TP=-1.5,"
            "aresample=48000"
        ),
    },
    {
        "name": "19-repeat-comfort-natural",
        "profiles": (
            {"length_scale": 0.98, "noise_scale": 0.48, "noise_w_scale": 0.62, "pitch_ratio": 1.11},
            {"length_scale": 1.04, "noise_scale": 0.50, "noise_w_scale": 0.68, "pitch_ratio": 1.07},
            {"length_scale": 0.96, "noise_scale": 0.49, "noise_w_scale": 0.64, "pitch_ratio": 1.13},
            {"length_scale": 0.99, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 1.10},
            {"length_scale": 1.06, "noise_scale": 0.50, "noise_w_scale": 0.66, "pitch_ratio": 1.05},
            {"length_scale": 1.02, "noise_scale": 0.54, "noise_w_scale": 0.72, "pitch_ratio": 1.03},
        ),
        "pauses_ms": (250, 650, 230, 590, 300),
        "eq_filter": (
            "highpass=f=68,"
            "equalizer=f=240:t=q:w=1.15:g=-2.2,"
            "equalizer=f=3300:t=q:w=1.0:g=-1.2,"
            "equalizer=f=4700:t=q:w=1.0:g=-0.7,"
            "loudnorm=I=-16:LRA=3:TP=-1.5,"
            "aresample=48000"
        ),
    },
    {
        "name": "20-repeat-comfort-airy",
        "profiles": (
            {"length_scale": 0.97, "noise_scale": 0.49, "noise_w_scale": 0.64, "pitch_ratio": 1.15},
            {"length_scale": 1.03, "noise_scale": 0.51, "noise_w_scale": 0.70, "pitch_ratio": 1.10},
            {"length_scale": 0.95, "noise_scale": 0.50, "noise_w_scale": 0.66, "pitch_ratio": 1.17},
            {"length_scale": 0.98, "noise_scale": 0.55, "noise_w_scale": 0.74, "pitch_ratio": 1.14},
            {"length_scale": 1.05, "noise_scale": 0.51, "noise_w_scale": 0.68, "pitch_ratio": 1.08},
            {"length_scale": 1.01, "noise_scale": 0.55, "noise_w_scale": 0.74, "pitch_ratio": 1.05},
        ),
        "pauses_ms": (230, 600, 210, 540, 280),
        "eq_filter": (
            "highpass=f=70,"
            "equalizer=f=240:t=q:w=1.15:g=-2.0,"
            "equalizer=f=3300:t=q:w=1.0:g=-1.4,"
            "equalizer=f=4700:t=q:w=1.0:g=-0.8,"
            "loudnorm=I=-16:LRA=3:TP=-1.5,"
            "aresample=48000"
        ),
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
    import re

    parts = re.findall(r".+?(?:[.!؟?]+|$)", text.strip(), flags=re.S)
    return [part.strip() for part in parts if part.strip()]


def _write_silence(
    handle: wave.Wave_write, *, ms: int, rate: int, channels: int, width: int
) -> None:
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

        _concat(sentence_paths, output, pauses_ms=[pause_ms] * (len(sentence_paths) - 1))


def _concat(paths: list[Path], output: Path, *, pauses_ms: list[int]) -> None:
    with wave.open(str(output), "wb") as dst:
        params = None
        for index, path in enumerate(paths):
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
                if index < len(pauses_ms):
                    _write_silence(
                        dst,
                        ms=int(pauses_ms[index]),
                        rate=params.framerate,
                        channels=params.nchannels,
                        width=params.sampwidth,
                    )


def _synthesize_dynamic(
    voice: PiperVoice,
    text: str,
    output: Path,
    *,
    profiles: tuple[dict, ...],
    pauses_ms: tuple[int, ...],
) -> None:
    sentences = _sentences(text)
    if len(sentences) != len(profiles):
        raise RuntimeError(
            f"dynamic profile mismatch: sentences={len(sentences)} profiles={len(profiles)}"
        )

    with tempfile.TemporaryDirectory(prefix="piper-dynamic-") as tmp:
        sentence_paths: list[Path] = []
        for index, (sentence, profile) in enumerate(zip(sentences, profiles)):
            path = Path(tmp) / f"{index:02d}.wav"
            config = SynthesisConfig(
                length_scale=float(profile["length_scale"]),
                noise_scale=float(profile["noise_scale"]),
                noise_w_scale=float(profile["noise_w_scale"]),
            )
            with wave.open(str(path), "wb") as wav:
                voice.synthesize_wav(sentence, wav, syn_config=config)
            sentence_paths.append(path)
        _concat(sentence_paths, output, pauses_ms=list(pauses_ms))


def _synthesize_performance(
    voice: PiperVoice,
    output: Path,
    *,
    profiles: tuple[dict, ...],
    pauses_ms: tuple[int, ...],
) -> None:
    if len(PERFORMANCE_TEXTS) != len(profiles):
        raise RuntimeError(
            f"performance profile mismatch: beats={len(PERFORMANCE_TEXTS)} profiles={len(profiles)}"
        )

    with tempfile.TemporaryDirectory(prefix="piper-performance-") as tmp:
        beat_paths: list[Path] = []
        for index, (beat, profile) in enumerate(zip(PERFORMANCE_TEXTS, profiles)):
            raw_path = Path(tmp) / f"{index:02d}-raw.wav"
            path = Path(tmp) / f"{index:02d}.wav"
            config = SynthesisConfig(
                length_scale=float(profile["length_scale"]),
                noise_scale=float(profile["noise_scale"]),
                noise_w_scale=float(profile["noise_w_scale"]),
            )
            with wave.open(str(raw_path), "wb") as wav:
                voice.synthesize_wav(beat, wav, syn_config=config)

            pitch_ratio = float(profile.get("pitch_ratio") or 1.0)
            if abs(pitch_ratio - 1.0) < 0.0001:
                path.write_bytes(raw_path.read_bytes())
            else:
                # Raise/lower each semantic beat slightly while preserving its
                # duration. This creates sentence-to-sentence intonation without
                # globally chipmunking or speeding up the voice.
                subprocess.run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(raw_path),
                        "-af",
                        (
                            f"rubberband=tempo=1:pitch={pitch_ratio:.6f}:"
                            "transients=smooth:detector=soft:phase=laminar:"
                            "window=long:smoothing=on:formant=preserved:pitchq=quality,"
                            "aresample=22050"
                        ),
                        "-c:a",
                        "pcm_s16le",
                        str(path),
                    ],
                    check=True,
                )
            beat_paths.append(path)
        _concat(beat_paths, output, pauses_ms=list(pauses_ms))


def _lighten_timbre(source: Path, destination: Path, *, audio_filter: str) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-af",
        audio_filter,
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    subprocess.run(command, check=True)


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

    tashkeel_available = hasattr(voice, "use_tashkeel")
    tashkeel_enabled = (
        bool(getattr(voice, "use_tashkeel", False)) if tashkeel_available else None
    )

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

    for variant in DYNAMIC_VARIANTS:
        final_path = output / f'{variant["name"]}.wav'
        with tempfile.TemporaryDirectory(prefix="piper-lighten-") as tmp:
            raw_path = Path(tmp) / "raw.wav"
            started = time.perf_counter()
            _synthesize_dynamic(
                voice,
                TEXT,
                raw_path,
                profiles=variant["profiles"],
                pauses_ms=variant["pauses_ms"],
            )
            _lighten_timbre(
                raw_path,
                final_path,
                audio_filter=str(variant["eq_filter"]),
            )
            generation_seconds = time.perf_counter() - started

        info = wav_info(final_path)
        results.append(
            {
                **variant,
                "mode": "per_sentence_dynamic_plus_light_eq",
                "generation_seconds": round(generation_seconds, 3),
                "realtime_factor": round(generation_seconds / info.duration_seconds, 3),
                "wav": asdict(info),
                "path": str(final_path),
            }
        )

    for variant in PERFORMANCE_VARIANTS:
        final_path = output / f'{variant["name"]}.wav'
        with tempfile.TemporaryDirectory(prefix="piper-performance-eq-") as tmp:
            raw_path = Path(tmp) / "raw.wav"
            started = time.perf_counter()
            _synthesize_performance(
                voice,
                raw_path,
                profiles=variant["profiles"],
                pauses_ms=variant["pauses_ms"],
            )
            _lighten_timbre(
                raw_path,
                final_path,
                audio_filter=str(variant["eq_filter"]),
            )
            generation_seconds = time.perf_counter() - started

        info = wav_info(final_path)
        results.append(
            {
                **variant,
                "mode": "six_semantic_beats_selective_diacritics_plus_light_eq",
                "performance_texts": PERFORMANCE_TEXTS,
                "generation_seconds": round(generation_seconds, 3),
                "realtime_factor": round(generation_seconds / info.duration_seconds, 3),
                "wav": asdict(info),
                "path": str(final_path),
            }
        )

    for variant in TARGET_MATCH_VARIANTS:
        final_path = output / f'{variant["name"]}.wav'
        with tempfile.TemporaryDirectory(prefix="piper-target-match-") as tmp:
            raw_path = Path(tmp) / "raw.wav"
            started = time.perf_counter()
            _synthesize_performance(
                voice,
                raw_path,
                profiles=variant["profiles"],
                pauses_ms=variant["pauses_ms"],
            )
            _lighten_timbre(
                raw_path,
                final_path,
                audio_filter=str(variant["eq_filter"]),
            )
            generation_seconds = time.perf_counter() - started

        info = wav_info(final_path)
        results.append(
            {
                **variant,
                "mode": "target_guided_semantic_pitch_pauses_mastering",
                "performance_texts": PERFORMANCE_TEXTS,
                "generation_seconds": round(generation_seconds, 3),
                "realtime_factor": round(generation_seconds / info.duration_seconds, 3),
                "wav": asdict(info),
                "path": str(final_path),
            }
        )

    for variant in COMFORT_FINAL_VARIANTS:
        final_path = output / f'{variant["name"]}.wav'
        with tempfile.TemporaryDirectory(prefix="piper-repeat-comfort-") as tmp:
            raw_path = Path(tmp) / "raw.wav"
            started = time.perf_counter()
            _synthesize_performance(
                voice,
                raw_path,
                profiles=variant["profiles"],
                pauses_ms=variant["pauses_ms"],
            )
            _lighten_timbre(
                raw_path,
                final_path,
                audio_filter=str(variant["eq_filter"]),
            )
            generation_seconds = time.perf_counter() - started

        info = wav_info(final_path)
        results.append(
            {
                **variant,
                "mode": "formant_preserved_pitch_semantic_breathing_soft_master",
                "performance_texts": PERFORMANCE_TEXTS,
                "generation_seconds": round(generation_seconds, 3),
                "realtime_factor": round(generation_seconds / info.duration_seconds, 3),
                "wav": asdict(info),
                "path": str(final_path),
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
        "listener_feedback_driving_final_round": {
            "clarity_estimate": "about_80_percent",
            "problem_1": "voice_heavy_and_uncomfortable",
            "problem_2": "same_cadence_across_sentences",
            "problem_3": "09_pronunciation_feels_compressed_and_too_serious_for_channel",
            "target_comparison": "mixed target suggests lighter pitch and wider motion; exact F0 is partially confounded by background music; target master measures about -16.1 LUFS and -1.2 dBTP",
            "comfort_followup": "remove_prior_3k_presence_boost_use_formant_preserved_pitch_and_no_extra_compressor",
        },
        "decision_rule": (
            "14 is the closest dry Piper direction. Compare 18/19/20 against 14. "
            "Prefer the lowest pitch lift that becomes repeat-listenable: preserved formants, "
            "softened 3-5 kHz presence, real semantic breathing, no extra compressor, and "
            "a 48 kHz master near -16 LUFS."
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
