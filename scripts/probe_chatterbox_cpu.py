#!/usr/bin/env python3
"""One-shot CPU probe for Chatterbox Multilingual V3.

This script is intentionally isolated from Clean V2 production code.
It measures whether the current upstream Chatterbox multilingual model
is practical on a standard GitHub Actions CPU runner.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import sys
import time
import traceback
from pathlib import Path

import psutil


TEXT = (
    "أحيانًا لا تحتاج إلى خطة جديدة، بل تحتاج إلى أن ترى يومك كما هو. "
    "ابدأ بخطوة صغيرة يمكنك تكرارها غدًا، ثم دع الاستمرار يصنع الفرق. "
    "التقدم الحقيقي لا يحتاج ضجيجًا، لكنه يحتاج قرارًا واضحًا يتكرر كل يوم."
)

OUT_DIR = Path("probe_artifacts")
OUT_WAV = OUT_DIR / "chatterbox-ar-v3.wav"
OUT_JSON = OUT_DIR / "chatterbox-cpu-probe.json"


def _peak_rss_mb() -> float:
    # Linux reports ru_maxrss in KiB.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def _rss_mb() -> float:
    return psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0)


def _base_result() -> dict:
    vm = psutil.virtual_memory()
    return {
        "status": "running",
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_logical": os.cpu_count(),
        "ram_total_gb": round(vm.total / (1024 ** 3), 2),
        "text": TEXT,
        "text_chars": len(TEXT),
        "language_id": "ar",
        "model": "ChatterboxMultilingualTTS",
        "t3_model": "v3",
        "device": "cpu",
        "rss_start_mb": round(_rss_mb(), 2),
    }


def _write(result: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print("PROBE_RESULT_JSON=" + json.dumps(result, ensure_ascii=False, sort_keys=True))


def main() -> int:
    result = _base_result()
    overall_started = time.perf_counter()

    try:
        import torch
        import soundfile as sf
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS

        threads = max(1, min(4, os.cpu_count() or 1))
        torch.set_num_threads(threads)
        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass

        result.update(
            {
                "torch_version": torch.__version__,
                "torch_threads": torch.get_num_threads(),
                "cuda_available": bool(torch.cuda.is_available()),
                "rss_before_load_mb": round(_rss_mb(), 2),
            }
        )

        load_started = time.perf_counter()
        model = ChatterboxMultilingualTTS.from_pretrained(
            device="cpu",
            t3_model="v3",
        )
        load_seconds = time.perf_counter() - load_started

        result.update(
            {
                "load_seconds": round(load_seconds, 3),
                "sample_rate": int(model.sr),
                "rss_after_load_mb": round(_rss_mb(), 2),
                "peak_rss_after_load_mb": round(_peak_rss_mb(), 2),
            }
        )

        generate_started = time.perf_counter()
        wav = model.generate(
            TEXT,
            language_id="ar",
            exaggeration=0.5,
            cfg_weight=0.5,
        )
        generation_seconds = time.perf_counter() - generate_started

        samples = wav.squeeze().detach().cpu().numpy()
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        sf.write(OUT_WAV, samples, model.sr, subtype="PCM_16")

        audio_duration_seconds = len(samples) / float(model.sr)
        real_time_factor = (
            generation_seconds / audio_duration_seconds
            if audio_duration_seconds > 0
            else None
        )

        result.update(
            {
                "status": "success",
                "generation_seconds": round(generation_seconds, 3),
                "audio_duration_seconds": round(audio_duration_seconds, 3),
                "real_time_factor": (
                    round(real_time_factor, 3)
                    if real_time_factor is not None
                    else None
                ),
                "rss_after_generate_mb": round(_rss_mb(), 2),
                "peak_rss_mb": round(_peak_rss_mb(), 2),
                "wav_path": str(OUT_WAV),
                "wav_bytes": OUT_WAV.stat().st_size,
            }
        )
        return_code = 0

    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "peak_rss_mb": round(_peak_rss_mb(), 2),
            }
        )
        return_code = 1

    finally:
        result["total_seconds"] = round(time.perf_counter() - overall_started, 3)
        _write(result)

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
