#!/usr/bin/env python3
"""Isolated Groq Orpheus Arabic Saudi probe.

Does not touch Clean V2. Uses the existing GROQ_API_KEY to generate the same
Modern Standard Arabic text with three male preset voices and records latency
and WAV metadata for manual listening.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

MODEL = "canopylabs/orpheus-arabic-saudi"
VOICES = ("abdullah", "fahad", "sultan")
TEXT = (
    "أحيانًا لا تحتاج إلى بداية جديدة، بل إلى خطوة صادقة تعيدك إلى طريقك. "
    "لا تنتظر أن تشعر بالدافع كاملًا؛ ابدأ بما تستطيع اليوم، ودع الاستمرار يصنع الفرق."
)
OUT_DIR = Path("probe_artifacts")


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
    return {
        "frames": frames,
        "sample_rate": rate,
        "channels": channels,
        "sample_width_bytes": width,
        "duration_seconds": round(frames / float(rate), 3) if rate else None,
        "bytes": path.stat().st_size,
    }


def synthesize(api_key: str, voice: str) -> dict:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"orpheus-arabic-{voice}.wav"
    body = json.dumps(
        {
            "model": MODEL,
            "voice": voice,
            "input": TEXT,
            "response_format": "wav",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/speech",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Isco-Orpheus-Probe/1",
        },
    )

    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = response.read()
            headers = dict(response.headers.items())
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return {
            "voice": voice,
            "status": "failed",
            "http_status": int(exc.code),
            "error": detail[:2000],
            "latency_seconds": round(time.perf_counter() - started, 3),
        }
    except Exception as exc:
        return {
            "voice": voice,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "latency_seconds": round(time.perf_counter() - started, 3),
        }

    latency = time.perf_counter() - started
    out_path.write_bytes(payload)

    result = {
        "voice": voice,
        "status": "success",
        "http_status": status,
        "latency_seconds": round(latency, 3),
        "content_type": headers.get("Content-Type"),
        "request_id": headers.get("x-request-id") or headers.get("X-Request-Id"),
        "path": str(out_path),
    }
    try:
        result.update(wav_info(out_path))
    except Exception as exc:
        result["wav_parse_error"] = f"{type(exc).__name__}: {exc}"
    return result


def main() -> int:
    api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("GROQ_API_KEY is missing")

    if len(TEXT) > 200:
        raise SystemExit(f"probe text too long: {len(TEXT)} chars")

    results = [synthesize(api_key, voice) for voice in VOICES]
    summary = {
        "model": MODEL,
        "text": TEXT,
        "text_chars": len(TEXT),
        "voices": list(VOICES),
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "orpheus-arabic-probe.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("PROBE_RESULT_JSON=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if all(r.get("status") == "success" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
