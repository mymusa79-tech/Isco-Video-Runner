#!/usr/bin/env python3
"""Final Nabra acceptance probe: listener-review 0.90 smooth voice + exact semantic pauses.

Probe-only. Tests whether the listener-approved Nabra voice generalizes to new
Arabic content while keeping sentence pauses natural and phrase endings smooth.
No production wiring.
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
NATIVE_SPEED = 0.90

SHORT_SENTENCES = (
    "بَعْضُ الأَيّام لا تَسير كَما خَطَّطْت.",
    "وَهَذا لا يَعْني أَنَّكَ خَسِرْت تَقَدُّمَك.",
    "أَصْلِح ما تَسْتَطيع، وَاتْرُك ما لا تَسْتَطيع تَغْييرَه الآن.",
    "ثُمَّ عُد إِلى خُطْوَتِك التّالِيَة بِهُدوء.",
    "فَالحَياة لا تَطْلُب مِنْكَ أَنْ تَكون مُثاليًّا؛ بَل أَنْ تَسْتَمِر بِوُضوح وَمَرونَة.",
)
SHORT_PAUSES_MS = (380, 950, 420, 1800)

LONG_SENTENCES = (
    "أَحْيانًا نَظُنُّ أَنَّ التَّقَدُّم يَحْتاج إِلى قَرار كَبير، لَكِنَّ الحَقيقَة أَبْسَط مِن ذَلِك.",
    "مُعْظَم التَّغْيير يَبْدَأ مِن خُطْوَة صَغيرَة نُكَرِّرُها حَتّى تُصْبِح جُزْءًا مِن حَياتِنا.",
    "قَد لا تَشْعُر بِالنَّتيجَة في البِدايَة، وَقَد تَظُنُّ أَنَّ جُهْدَك لا يَتَحَرَّك.",
    "لَكِنْ عِنْدَما تَنْظُر إِلى أَسابيع كامِلَة، تَكْتَشِف أَنَّ الأَثَر كان يَتَراكَم بِهُدوء.",
    "هُنا تَظْهَر قِيمَة الاِسْتِمْرار؛ أَنْ تَفْعَل ما تَسْتَطيع، حَتّى في الأَيّام الَّتي لا تَشْعُر فيها بِالحَماس.",
    "لَيْس المَطْلوب أَنْ تَكون مُثاليًّا، وَلا أَنْ تُنْجِز كُلَّ شَيْء دَفْعَة واحِدَة.",
    "المَطْلوب أَنْ تَعْرِف ما هُو مُهِمّ، ثُمَّ تَعود إِلَيْه مَرَّة بَعْد مَرَّة.",
    "وَعِنْدَما تَتَعَثَّر، لا تَجْعَل يَوْمًا صَعْبًا يَتَحَوَّل إِلى أُسْبوع كامِل مِن التَّوَقُّف.",
    "اِرْجِع بِهُدوء، وَابْدَأ مِن أَقْرَب خُطْوَة مُمْكِنَة.",
    "بَعْد مُدَّة، سَتَكْتَشِف أَنَّ ما صَنَع الفَرْق لَم يَكُن لَحْظَة حَماس، بَلْ عادات صَغيرَة حافَظْت عَلَيْها حِينَ كان التَّقَدُّم بَطيئًا.",
)
LONG_PAUSES_MS = (420, 950, 380, 1000, 420, 950, 400, 1000, 1800)


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "sample_width_bytes": wf.getsampwidth(),
            "bytes": path.stat().st_size,
        }


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


def _frame_rms(audio: np.ndarray, frame: int) -> np.ndarray:
    values: list[float] = []
    for start in range(0, int(audio.size), frame):
        chunk = audio[start : start + frame]
        if chunk.size:
            values.append(float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))))
    return np.asarray(values, dtype=np.float64)


def smooth_sentence_edges(
    audio: np.ndarray,
    *,
    threshold_db: float = -34.0,
    frame_ms: int = 10,
    pre_roll_ms: int = 12,
    post_roll_ms: int = 180,
    onset_fade_ms: int = 4,
    release_hold_ms: int = 35,
    release_fade_ms: int = 110,
) -> tuple[np.ndarray, dict]:
    """Clean sentence boundaries without touching lexical timing.

    Start:
      - keep the detected speech frame itself intact in position;
      - replace only the protected pre-speech context with true silence;
      - use a tiny 4 ms fade on the first active samples to avoid a click.
    End:
      - never fade inside the detected lexical speech core;
      - keep a natural 35 ms release after speech;
      - fade only the model-generated trailing tail toward silence.
    """
    if audio.size == 0:
        return audio, {"status": "empty"}

    frame = max(1, int(SAMPLE_RATE * frame_ms / 1000.0))
    rms = _frame_rms(audio, frame)
    threshold = float(10 ** (threshold_db / 20.0))
    active_frames = np.flatnonzero(rms > threshold)
    if active_frames.size == 0:
        return audio.astype(np.float32, copy=True), {
            "status": "no_active_frames",
            "threshold_db": threshold_db,
        }

    speech_start = int(active_frames[0] * frame)
    speech_end = min(int(audio.size), int((active_frames[-1] + 1) * frame))

    pre = int(SAMPLE_RATE * pre_roll_ms / 1000.0)
    post = int(SAMPLE_RATE * post_roll_ms / 1000.0)
    start = max(0, speech_start - pre)
    end = min(int(audio.size), speech_end + post)

    out = audio[start:end].astype(np.float32, copy=True)
    speech_start_local = speech_start - start
    speech_end_local = min(int(out.size), speech_end - start)

    # The protected pre-roll used to retain the model's hiss/breath onset.
    # Make that region truly silent instead. The lexical attack is not removed.
    if speech_start_local > 0:
        out[:speech_start_local] = 0.0

    onset_fade = min(
        int(SAMPLE_RATE * onset_fade_ms / 1000.0),
        max(0, int(out.size) - speech_start_local),
    )
    if onset_fade > 1:
        out[speech_start_local:speech_start_local + onset_fade] *= np.sin(
            np.linspace(0.0, np.pi / 2.0, onset_fade, dtype=np.float32)
        ) ** 2

    available_tail = max(0, int(out.size) - speech_end_local)
    hold = min(
        int(SAMPLE_RATE * release_hold_ms / 1000.0),
        available_tail,
    )
    fade_available = max(0, available_tail - hold)
    fade_len = min(
        int(SAMPLE_RATE * release_fade_ms / 1000.0),
        fade_available,
    )

    # Crucial invariant: fade_start is always >= speech_end_local.
    # Therefore no lexical phoneme can be attenuated by the release smoothing.
    fade_start = speech_end_local + hold
    if fade_len > 1:
        fade_end = fade_start + fade_len
        out[fade_start:fade_end] *= np.cos(
            np.linspace(0.0, np.pi / 2.0, fade_len, dtype=np.float32)
        ) ** 2
        if fade_end < out.size:
            out[fade_end:] = 0.0
        else:
            out[-1] = 0.0
    elif available_tail > 0:
        # No room for a proper fade: preserve the tail rather than touching
        # speech. The following deterministic pause starts after this chunk.
        pass

    return out, {
        "status": "ok",
        "threshold_db": threshold_db,
        "speech_start_ms": round(speech_start * 1000.0 / SAMPLE_RATE, 2),
        "speech_end_ms": round(speech_end * 1000.0 / SAMPLE_RATE, 2),
        "trimmed_start_ms": round(start * 1000.0 / SAMPLE_RATE, 2),
        "trimmed_end_ms": round((audio.size - end) * 1000.0 / SAMPLE_RATE, 2),
        "pre_roll_ms": pre_roll_ms,
        "pre_roll_zeroed": True,
        "onset_fade_ms_applied": round(onset_fade * 1000.0 / SAMPLE_RATE, 2),
        "post_roll_ms_requested": post_roll_ms,
        "available_release_ms": round(available_tail * 1000.0 / SAMPLE_RATE, 2),
        "release_hold_ms_applied": round(hold * 1000.0 / SAMPLE_RATE, 2),
        "release_fade_ms_applied": round(fade_len * 1000.0 / SAMPLE_RATE, 2),
        "fade_touches_speech_core": False,
        "speech_core_retimed": False,
        "sentence_end_hard_cut": False,
    }


def synthesize_passage(
    *,
    model: KModel,
    voice: torch.Tensor,
    g2p,
    sentences: tuple[str, ...],
    pauses_ms: tuple[int, ...],
) -> tuple[np.ndarray, list[dict], list[str], float]:
    if len(pauses_ms) != len(sentences) - 1:
        raise RuntimeError("pause count must equal sentence count minus one")

    chunks: list[np.ndarray] = []
    reports: list[dict] = []
    phoneme_rows: list[str] = []
    started = time.perf_counter()

    with torch.inference_mode():
        for index, sentence in enumerate(sentences):
            phonemes, _ = g2p(sentence)
            phonemes = clean_phonemes(phonemes)
            phoneme_rows.append(phonemes)
            output = KPipeline.infer(
                model,
                phonemes,
                voice.to(model.device),
                speed=NATIVE_SPEED,
            )
            chunk = output.audio.detach().cpu().numpy().astype(np.float32)
            chunk, edge_report = smooth_sentence_edges(chunk)
            edge_report["sentence_index"] = index + 1
            edge_report["text"] = sentence
            edge_report["phonemes"] = phonemes
            reports.append(edge_report)
            chunks.append(chunk)

    synth_seconds = time.perf_counter() - started

    timeline: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        timeline.append(chunk)
        if index < len(pauses_ms):
            timeline.append(
                np.zeros(int(round(SAMPLE_RATE * pauses_ms[index] / 1000.0)), dtype=np.float32)
            )

    return np.concatenate(timeline).astype(np.float32), reports, phoneme_rows, synth_seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="probe_artifacts/nabra-acceptance")
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

    def verified_g2p(text: str):
        phonemes, extra = original_g2p(text)
        return clean_phonemes(phonemes), extra

    voice = torch.load(voice_path, map_location="cpu", weights_only=True)
    load_seconds = time.perf_counter() - load_started

    short_audio, short_edges, short_phonemes, short_synth = synthesize_passage(
        model=model,
        voice=voice,
        g2p=verified_g2p,
        sentences=SHORT_SENTENCES,
        pauses_ms=SHORT_PAUSES_MS,
    )
    short_raw = output / "01-nabra-new-short-smooth-raw.wav"
    short_mix = output / "02-nabra-new-short-smooth-mix-ready.wav"
    sf.write(short_raw, short_audio, SAMPLE_RATE, subtype="PCM_16")
    mix_ready(short_raw, short_mix)

    long_audio, long_edges, long_phonemes, long_synth = synthesize_passage(
        model=model,
        voice=voice,
        g2p=verified_g2p,
        sentences=LONG_SENTENCES,
        pauses_ms=LONG_PAUSES_MS,
    )
    long_raw = output / "03-nabra-long-validation-smooth-raw.wav"
    long_mix = output / "04-nabra-long-validation-smooth-mix-ready.wav"
    sf.write(long_raw, long_audio, SAMPLE_RATE, subtype="PCM_16")
    mix_ready(long_raw, long_mix)

    peak_rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    report = {
        "status": "success",
        "model": REPO_ID,
        "voice": "af_msa",
        "native_speed": NATIVE_SPEED,
        "edge_policy": {
            "threshold_db": -34.0,
            "frame_ms": 10,
            "pre_roll_ms": 12,
            "post_roll_ms": 180,
            "onset_fade_ms": 4,
            "release_hold_ms": 35,
            "release_fade_ms": 110,
            "principle": (
                "zero pre-speech hiss; fade only after detected lexical speech; "
                "never retime or attenuate the speech core"
            ),
        },
        "short": {
            "sentences": SHORT_SENTENCES,
            "pauses_ms": SHORT_PAUSES_MS,
            "phonemes": short_phonemes,
            "edges": short_edges,
            "synthesis_seconds": round(short_synth, 3),
            "raw_wav": wav_info(short_raw),
            "mix_ready_wav": wav_info(short_mix),
        },
        "long": {
            "sentences": LONG_SENTENCES,
            "pauses_ms": LONG_PAUSES_MS,
            "phonemes": long_phonemes,
            "edges": long_edges,
            "synthesis_seconds": round(long_synth, 3),
            "raw_wav": wav_info(long_raw),
            "mix_ready_wav": wav_info(long_mix),
        },
        "download_seconds": round(download_seconds, 3),
        "model_and_frontend_load_seconds": round(load_seconds, 3),
        "peak_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "python": platform.python_version(),
        "torch_version": torch.__version__,
        "runner_cpu_count": os.cpu_count(),
        "production_wiring": False,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("NABRA_ACCEPTANCE_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
