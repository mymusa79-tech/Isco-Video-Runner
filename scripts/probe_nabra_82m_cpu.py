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
    "أَحْيَانًا، لَا تَحْتَاج إِلَى بِدَايَة جَدِيدَة.",
    "بَلْ تَحْتَاج إِلَى خُطْوَة صَادِقَة تُعِيدُكَ إِلَى طَرِيقِكَ.",
    "لَا تَنْتَظِرْ أَنْ يَأْتِيَ الدَّافِع كَامِلًا.",
    "اِبْدَأْ بِمَا تَسْتَطِيع الْيَوْم.",
    "فَالاسْتِمْرَار الْهَادِئ، حِينَ يَتَكَرَّر كُلَّ يَوْم، يَصْنَع فَرْقًا أَكْبَر مِمَّا تَتَخَيَّل.",
)
PAUSES_MS = (260, 420, 340, 500)

# Direct phoneme lock for the listener-sensitive phrases. This bypasses Arabic
# G2P/diacritization entirely during synthesis, uses spoken MSA endings (no
# heavy case inflection), and generates the whole passage in one call so there
# is only one model onset.
LOCKED_PHONEMES = (
    "ʔˈaħjaːnˌan, laː tˈaħtaːʤ ʔˈilaː bidˈaːja ʤadˈiːda. "
    "bal tˈaħtaːʤ ʔˈilaː χˈutwa sˈaːdiqa tuʕˈiːduk ʔˈilaː tarˈiːqik. "
    "laː tˈantaðˌir ʔˈan jˈaʔtiː ʔadˈaːfiʕ kˈaːmilan. "
    "ʔˈibdaʔ bimˌaː tastˈatiːʕ ʔaljˈaum. "
    "falˌistimrˈaːr alhˈaːdiʔ, ħˈiːna jˌatakˈarrar kˈull jˈaum, "
    "jˈasnaʕ farqˌan ʔˈakbar mˈimmaː tˌataχaˈiːal."
)


def soften_segment_onset(audio: np.ndarray) -> np.ndarray:
    """Gently fade the model's phrase-start onset without cutting speech.

    The previous probe trimmed samples before detected speech and damaged Arabic
    initial consonants. This version preserves every sample and applies only a
    45 ms linear fade-in, which reduces the breath/hiss-like synthetic onset.
    """
    if audio.size == 0:
        return audio
    fade_frames = min(audio.size, int(SAMPLE_RATE * 0.045))
    if fade_frames <= 1:
        return audio
    out = audio.astype(np.float32, copy=True)
    out[:fade_frames] *= np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
    return out


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

    # Pronunciation-locked single-pass synthesis for final listening check.
    # KPipeline.infer accepts raw phonemes and the already-loaded voice pack.
    locked_started = time.perf_counter()
    with torch.inference_mode():
        locked_output = KPipeline.infer(
            model,
            LOCKED_PHONEMES,
            voice.to(model.device),
            speed=NATIVE_SPEED,
        )
    locked_seconds = time.perf_counter() - locked_started
    locked_audio = locked_output.audio.detach().cpu().numpy().astype(np.float32)
    # Only soften the one global model onset; nothing is cut.
    locked_audio = soften_segment_onset(locked_audio)

    locked_raw_path = output / "08-nabra-pronunciation-locked-raw.wav"
    sf.write(locked_raw_path, locked_audio, SAMPLE_RATE, subtype="PCM_16")
    locked_final_path = output / "09-nabra-pronunciation-locked-mix-ready.wav"
    mix_ready(locked_raw_path, locked_final_path)

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
            chunks.append(soften_segment_onset(np.concatenate(segment_chunks).astype(np.float32)))

    synth_seconds = time.perf_counter() - synth_started

    audio = add_silence(chunks, PAUSES_MS)
    raw_path = output / "08-nabra-82m-spoken-msa-raw.wav"
    sf.write(raw_path, audio, SAMPLE_RATE, subtype="PCM_16")

    final_path = output / "09-nabra-82m-spoken-msa-mix-ready.wav"
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
        "manual_tashkeel": "spoken_msa_verified",
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
        "pronunciation_locked_phonemes": LOCKED_PHONEMES,
        "pronunciation_locked_synthesis_seconds": round(locked_seconds, 3),
        "pronunciation_locked_raw_wav": wav_info(locked_raw_path),
        "pronunciation_locked_mix_ready_wav": wav_info(locked_final_path),
        "notes": [
            "official Nabra repo_id and disable_complex inference path",
            "manually verified spoken-MSA tashkeel: lexical vowels preserved, unnecessary final case endings omitted",
            "native Nabra speed=0.94; no atempo or post speed change",
            "semantic pauses inserted only between complete ideas",
            "no audio samples are trimmed; only a 45 ms fade-in reduces phrase-start hiss",
            "no EQ, pitch shift, compressor, or voice retiming",
            "mix-ready file is loudness normalization plus 48 kHz resample only",
            "pronunciation-locked sample bypasses G2P and uses one continuous phoneme sequence",
            "problem phrases use spoken-MSA phonemes without heavy case endings",
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
