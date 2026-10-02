#!/usr/bin/env python3
"""Bounded long-form comfort probe for the approved Kareem direction.

This does not touch Clean V2 production. It keeps the listener-selected
short-form timbre/mastering direction but uses a calmer long-form cadence.
No provider calls, no model training, no atempo.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import wave
from pathlib import Path

from piper import PiperVoice
from piper.config import SynthesisConfig

PHRASES = (
    "أحيانًا نبحث عن التغيير الكبير، مع أن ما نحتاجه فعلًا هو خطوة صغيرة نستطيع تكرارها.",
    "ليست المشكلة دائمًا في أنك لا تعرف الطريق؛ أحيانًا أنت تعرفه جيدًا، لكنك تحاول أن تقطعه دفعة واحدة.",
    "وهنا يبدأ الإرهاق.",
    "حين تجعل كل يوم اختبارًا حاسمًا، يصبح أبسط تعثر وكأنه دليل على أنك لم تتقدم.",
    "لكن الحقيقة أهدأ من ذلك.",
    "التقدم الحقيقي قد يكون أن تعود بعد يوم صعب، وأن تفعل القليل بدل أن تترك كل شيء.",
    "قد يكون أن تنام أفضل، أو تتحرك أكثر، أو تنجز مهمة واحدة كانت تؤجل منذ أيام.",
    "هذه الأشياء لا تبدو بطولية، لكنها حين تتكرر تغيّر شكل حياتك من الداخل.",
    "لا تحتاج أن تشعر بالحماس طوال الوقت.",
    "يكفي أن تبني طريقة تستطيع العودة إليها حين يختفي الحماس.",
    "ومع الوقت، لن يكون السؤال: هل أستطيع أن أبدأ؟",
    "بل: كيف أحافظ على هذا الهدوء الذي يجعلني أستمر؟",
)

# Calmer than Short 23: smaller contrast, longer idea-level rests, almost no
# isolated-word treatment. The third and fifth phrases are intentionally short
# breaths to prevent a lecture-like continuous read.
PROFILES = (
    {"length_scale": 0.98, "noise_scale": 0.54, "noise_w_scale": 0.72, "gain_db": 0.2},
    {"length_scale": 1.03, "noise_scale": 0.49, "noise_w_scale": 0.64, "gain_db": -0.2},
    {"length_scale": 1.07, "noise_scale": 0.44, "noise_w_scale": 0.56, "gain_db": -0.5},
    {"length_scale": 1.02, "noise_scale": 0.50, "noise_w_scale": 0.66, "gain_db": -0.1},
    {"length_scale": 1.08, "noise_scale": 0.43, "noise_w_scale": 0.54, "gain_db": -0.6},
    {"length_scale": 1.00, "noise_scale": 0.53, "noise_w_scale": 0.70, "gain_db": 0.1},
    {"length_scale": 0.99, "noise_scale": 0.55, "noise_w_scale": 0.74, "gain_db": 0.2},
    {"length_scale": 1.04, "noise_scale": 0.49, "noise_w_scale": 0.64, "gain_db": -0.1},
    {"length_scale": 1.06, "noise_scale": 0.45, "noise_w_scale": 0.58, "gain_db": -0.4},
    {"length_scale": 0.99, "noise_scale": 0.54, "noise_w_scale": 0.72, "gain_db": 0.2},
    {"length_scale": 1.03, "noise_scale": 0.48, "noise_w_scale": 0.62, "gain_db": -0.2},
    {"length_scale": 1.05, "noise_scale": 0.50, "noise_w_scale": 0.66, "gain_db": -0.1},
)

PAUSES_MS = (420, 520, 300, 560, 320, 500, 470, 620, 360, 520, 650)

MASTER_FILTER = (
    "highpass=f=64,"
    "equalizer=f=210:t=q:w=1.10:g=-1.4,"
    "equalizer=f=2800:t=q:w=1.0:g=-0.8,"
    "equalizer=f=5000:t=q:w=0.9:g=-1.0,"
    "loudnorm=I=-17:LRA=4:TP=-1.5,"
    "aresample=48000"
)


def _write_silence(handle, *, ms: int, rate: int, channels: int, width: int) -> None:
    frames = int(rate * (ms / 1000.0))
    handle.writeframes(b"\x00" * frames * channels * width)


def _concat(paths: list[Path], output: Path) -> None:
    with wave.open(str(output), "wb") as dst:
        params = None
        for index, path in enumerate(paths):
            with wave.open(str(path), "rb") as src:
                if params is None:
                    params = src.getparams()
                    dst.setparams(params)
                dst.writeframes(src.readframes(src.getnframes()))
                if index < len(PAUSES_MS):
                    _write_silence(
                        dst,
                        ms=PAUSES_MS[index],
                        rate=params.framerate,
                        channels=params.nchannels,
                        width=params.sampwidth,
                    )


def _wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / wf.getframerate(), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "bytes": path.stat().st_size,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--output", default="probe_artifacts/piper-kareem-long-comfort")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    model = Path(args.model)
    voice = PiperVoice.load(str(model), config_path=str(model) + ".json")

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="piper-long-comfort-") as tmp:
        phrase_paths: list[Path] = []
        for index, (phrase, profile) in enumerate(zip(PHRASES, PROFILES)):
            raw = Path(tmp) / f"{index:02d}-raw.wav"
            adjusted = Path(tmp) / f"{index:02d}.wav"
            config = SynthesisConfig(
                length_scale=float(profile["length_scale"]),
                noise_scale=float(profile["noise_scale"]),
                noise_w_scale=float(profile["noise_w_scale"]),
            )
            with wave.open(str(raw), "wb") as wav:
                voice.synthesize_wav(phrase, wav, syn_config=config)
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(raw),
                    "-af", f"volume={float(profile['gain_db']):+.3f}dB",
                    "-c:a", "pcm_s16le", str(adjusted),
                ],
                check=True,
            )
            phrase_paths.append(adjusted)

        joined = Path(tmp) / "joined.wav"
        _concat(phrase_paths, joined)

        final_path = output / "26-long-comfort-calm.wav"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(joined),
                "-af", MASTER_FILTER,
                "-c:a", "pcm_s16le", str(final_path),
            ],
            check=True,
        )

    elapsed = time.perf_counter() - started
    info = _wav_info(final_path)
    report = {
        "status": "success",
        "voice": "ar_JO-kareem-medium",
        "mode": "long_form_calm_from_listener_selected_23_25_direction",
        "phrase_count": len(PHRASES),
        "generation_seconds": round(elapsed, 3),
        "realtime_factor": round(elapsed / info["duration_seconds"], 3),
        "wav": info,
        "master_target": {"lufs": -17, "lra": 4, "true_peak_db": -1.5},
        "notes": [
            "calmer cadence than short 23/25",
            "no pitch shift",
            "no atempo",
            "idea-level pauses only",
            "slightly more headroom for long-form music mix",
        ],
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("PIPER_LONG_COMFORT_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
