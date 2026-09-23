#!/usr/bin/env python3
"""Bounded CPU benchmark/refinement of Nabra-82M.

Experimental only. Uses the official current Nabra inference path, then makes
one listener-guided comfort sample: manually corrected tashkeel, slightly
slower native model speed, and semantic pauses. No production wiring.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from huggingface_hub import hf_hub_download
from arabic_g2p import EXTRA_SYMBOLS, clean_phonemes
from kokoro import KModel, KPipeline
from kokoro import pipeline as kpipeline_mod

REPO_ID = "oddadmix/Nabra-82M-v0.1"
SAMPLE_RATE = 24000
NATIVE_SPEED = 0.94

# Manually corrected MSA tashkeel. This intentionally bypasses Camel's wrong
# guesses seen in the first probe (e.g. أَنَّ, أَبْدَأ, فِرَقًا).
SEGMENTS = (
    "أحيانًا، لا تحتاج إلى بداية جديدة.",
    "بل تحتاج إلى خُطوة صادقة تُعيدك إلى طريقك.",
    "لا تنتظر أَنْ يأتي الدافع كاملًا.",
    "اِبْدَأْ بما تستطيع اليوم.",
    "فالاستمرار الهادئ، حين يتكرر كل يوم، يصنع فَرْقًا أكبر مما تتخيل.",
)
PAUSES_MS = (260, 420, 340, 500)


def trim_segment_onset(audio: np.ndarray) -> np.ndarray:
    """Remove only the tiny synthetic onset before each independently generated phrase.

    Nabra can emit a short breath/hiss-like onset when a fresh phrase starts.
    Detect the first stable speech frame relative to the phrase RMS and keep a
    small 25 ms pre-roll so consonant attacks are not clipped.
    """
    if audio.size < SAMPLE_RATE // 10:
        return audio
    frame = max(1, int(SAMPLE_RATE * 0.010))
    rms = []
    for start in range(0, min(audio.size, int(SAMPLE_RATE * 0.45)), frame):
        chunk = audio[start:start + frame]
        if chunk.size:
            rms.append(float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))))
    if not rms:
        return audio
    peak = max(rms)
    if peak <= 1e-6:
        return audio
    threshold = max(0.003, peak * 0.18)
    stable = None
    for i in range(max(0, len(rms) - 2)):
        if rms[i] >= threshold and rms[i + 1] >= threshold:
            stable = i
            break
    if stable is None:
        return audio
    cut = max(0, stable * frame - int(SAMPLE_RATE * 0.025))
    return audio[cut:]


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "sample_width_bytes": wf.getsampwidth(),
            "bytes": path.stat().st_size,
        }


def add_silence(chunks: list[np.ndarray], pauses_ms: tuple[int, ...]) -> np.ndarray:
    out: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        out.append(chunk.astype(np.float32))
        if index < len(pauses_ms):
            frames = int(SAMPLE_RATE * pauses_ms[index] / 1000.0)
            out.append(np.zeros(frames, dtype=np.float32))
    return np.concatenate(out).astype(np.float32)


def mix_ready(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-af", "loudnorm=I=-16.2:LRA=3:TP=-1.5,aresample=48000",
            "-c:a", "pcm_s16le",
            str(destination),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="probe_artifacts/nabra-82m-cpu")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    download_started = time.perf_counter()
    model_path = hf_hub_download(REPO_ID, "kokoro_arabic.pth")
    config_path = hf_hub_download(REPO_ID, "config.json")
    voice_path = hf_hub_download(REPO_ID, "af_msa.pt")
    download_seconds = time.perf_counter() - download_started

    load_started = time.perf_counter()
    model = KModel(
        repo_id=REPO_ID,
        config=config_path,
        model=model_path,
        disable_complex=True,
    ).eval()
    model.vocab.update(EXTRA_SYMBOLS)

    kpipeline_mod.LANG_CODES.setdefault("ar", "ar")
    pipeline = KPipeline(lang_code="ar", repo_id=REPO_ID, model=model)
    original_g2p = pipeline.g2p

    def clean_arabic_g2p(text: str):
        phonemes, extra = original_g2p(text)
        return clean_phonemes(phonemes), extra

    pipeline.g2p = clean_arabic_g2p
    voice = torch.load(voice_path, map_location="cpu", weights_only=True)
    load_seconds = time.perf_counter() - load_started

    synth_started = time.perf_counter()
    chunks: list[np.ndarray] = []
    emitted_phonemes: list[str] = []

    with torch.inference_mode():
        for segment in SEGMENTS:
            segment_chunks = []
            for _, phonemes, audio in pipeline(segment, voice=voice, speed=NATIVE_SPEED):
                emitted_phonemes.append(phonemes)
                segment_chunks.append(audio.detach().cpu().numpy())
            if not segment_chunks:
                raise RuntimeError(f"Nabra emitted no audio for segment: {segment}")
            chunks.append(trim_segment_onset(np.concatenate(segment_chunks).astype(np.float32)))

    synth_seconds = time.perf_counter() - synth_started

    audio = add_silence(chunks, PAUSES_MS)
    raw_path = output / "04-nabra-82m-final-clean-raw.wav"
    sf.write(raw_path, audio, SAMPLE_RATE, subtype="PCM_16")

    final_path = output / "05-nabra-82m-final-clean-mix-ready.wav"
    mix_ready(raw_path, final_path)

    raw = wav_info(raw_path)
    final = wav_info(final_path)
    peak_rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    report = {
        "status": "success",
        "model": REPO_ID,
        "voice": "af_msa",
        "device": "cpu",
        "official_inference_path": True,
        "manual_tashkeel": "selective_only",
        "native_speed": NATIVE_SPEED,
        "segments": SEGMENTS,
        "pauses_ms": PAUSES_MS,
        "phonemes_emitted": emitted_phonemes,
        "download_seconds": round(download_seconds, 3),
        "model_and_frontend_load_seconds": round(load_seconds, 3),
        "synthesis_seconds": round(synth_seconds, 3),
        "raw_realtime_factor": round(synth_seconds / raw["duration_seconds"], 3),
        "peak_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "python": platform.python_version(),
        "runner_cpu_count": os.cpu_count(),
        "raw_wav": raw,
        "mix_ready_wav": final,
        "notes": [
            "official Nabra repo_id and disable_complex inference path",
            "selective MSA tashkeel only on ambiguous/problem words; avoids robotic full case endings",
            "native Nabra speed=0.94; no atempo or post speed change",
            "semantic pauses inserted only between complete ideas",
            "per-segment synthetic onset is trimmed with conservative 25 ms pre-roll",
            "no EQ, pitch shift, compressor, or voice retiming",
            "mix-ready file is loudness normalization plus 48 kHz resample only",
            "experimental only; no Clean V2 production wiring",
        ],
    }

    (output / "report-refined.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("NABRA_REFINED_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
