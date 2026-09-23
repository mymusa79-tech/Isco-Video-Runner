#!/usr/bin/env python3
"""One bounded CPU benchmark of Nabra-82M against the current Kareem reference text.

Experimental only. No production wiring, no provider/API calls, no secrets.
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
from arabic_g2p import ArabicG2P, EXTRA_SYMBOLS, clean_phonemes, normalize_text
from kokoro import KModel, KPipeline
from kokoro import pipeline as kpipeline_mod

REPO_ID = "oddadmix/Nabra-82M-v0.1"
BASE_REPO = "hexgrad/Kokoro-82M"
SAMPLE_RATE = 24000

# Same semantic sample used to converge on the Kareem 23/25 direction.
TEXT = (
    "أحيانًا، لا تحتاج إلى بداية جديدة. "
    "بل تحتاج إلى خطوة صادقة تعيدك إلى طريقك. "
    "لا تنتظر أن يأتي الدافع كاملًا. "
    "ابدأ بما تستطيع اليوم. "
    "فالاستمرار الهادئ، حين يتكرر كل يوم، "
    "يصنع فرقًا أكبر مما تتخيل."
)


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "sample_width_bytes": wf.getsampwidth(),
            "bytes": path.stat().st_size,
        }


def run_ffmpeg(source: Path, destination: Path) -> None:
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
        repo_id=BASE_REPO,
        config=config_path,
        model=model_path,
    ).eval()
    model.vocab.update(EXTRA_SYMBOLS)

    kpipeline_mod.LANG_CODES.setdefault("ar", "ar")
    pipeline = KPipeline(lang_code="ar", repo_id=BASE_REPO, model=model)

    g2p = ArabicG2P(diacritize=True)
    original_g2p = pipeline.g2p

    def arabic_frontend(text: str):
        normalized, _ = normalize_text(text)
        diacritized = g2p.diacritize(normalized)
        phonemes, extra = original_g2p(diacritized)
        return clean_phonemes(phonemes), extra

    pipeline.g2p = arabic_frontend
    voice = torch.load(voice_path, map_location="cpu", weights_only=True)
    load_seconds = time.perf_counter() - load_started

    normalized, latin_dropped = normalize_text(TEXT)
    diacritized = g2p.diacritize(normalized)
    preview_phonemes, _ = original_g2p(diacritized)
    preview_phonemes = clean_phonemes(preview_phonemes)

    synth_started = time.perf_counter()
    chunks = []
    emitted_phonemes = []
    with torch.inference_mode():
        for _, phonemes, audio in pipeline(TEXT, voice=voice, speed=1.0):
            emitted_phonemes.append(phonemes)
            chunks.append(audio.detach().cpu().numpy())
    synth_seconds = time.perf_counter() - synth_started

    if not chunks:
        raise RuntimeError("Nabra emitted no audio")

    audio = np.concatenate(chunks).astype(np.float32)
    raw_path = output / "00-nabra-82m-raw.wav"
    sf.write(raw_path, audio, SAMPLE_RATE, subtype="PCM_16")

    mix_path = output / "01-nabra-82m-mix-ready.wav"
    run_ffmpeg(raw_path, mix_path)

    raw = wav_info(raw_path)
    mix = wav_info(mix_path)
    peak_rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    report = {
        "status": "success",
        "model": REPO_ID,
        "voice": "af_msa",
        "device": "cpu",
        "text": TEXT,
        "normalized_text": normalized,
        "diacritized_text": diacritized,
        "phonemes_preview": preview_phonemes,
        "phonemes_emitted": emitted_phonemes,
        "latin_runs_dropped": latin_dropped,
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
        "mix_ready_wav": mix,
        "notes": [
            "single synthesis pass",
            "official Nabra Arabic G2P path with camel-tools diacritization",
            "mix-ready file is only loudness normalization plus 48 kHz resample",
            "no EQ, pitch shift, compressor, atempo, or voice retiming",
            "experimental only; no Clean V2 production wiring",
        ],
    }

    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("NABRA_CPU_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
