"""Owner listening test: one Iapetus voice throughout a Nidaa Short.

Only called in a manually isolated GitHub Actions test workflow. Never publishes.
"""
from __future__ import annotations

import json
import time
import wave
from pathlib import Path

from clean_v2.identity_sequence import PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION
from clean_v2.media import (
    GEMINI38_NIDAA_SHORT_PROVIDER,
    GEMINI38_TTS_MODEL,
    GeminiOnlyVoiceSynthesizer,
)

# Compact test copy, not an approved public production script.
VOICE_UNITS = (
    ("hook", "أنت لا تحتاج إلى انتظار الثقة حتى تبدأ!", "nidaa_short_why_reframe"),
    ("prayer", PRAYER_SENTENCE, "nidaa_short_identity"),
    ("channel_identity", SHORT_CHANNEL_DEFINITION, "nidaa_short_identity"),
    ("topic", "كم مرة أجّلت خطوة لأنك لم تشعر أنك مستعد؟ الاستعداد لا يسبق التجربة دائمًا. "
     "اختر خطوة صغيرة اليوم، وأنجزها قبل أن تنتظر الشعور المثالي.", "nidaa_short_why_reframe"),
)

def main() -> None:
    import os

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is required")
    out = Path("iapetus-sequence")
    out.mkdir(exist_ok=True)
    synth = GeminiOnlyVoiceSynthesizer(api_key, tts_model=GEMINI38_TTS_MODEL)
    info = []
    wave_segments: list[tuple[bytes, float]] = []
    for index, (role, spoken, mode) in enumerate(VOICE_UNITS, 1):
        path = out / f"{index:02d}_{role}.wav"
        synth.synthesize(spoken, path, performance_mode=mode)
        if synth.last_provider != GEMINI38_NIDAA_SHORT_PROVIDER:
            raise RuntimeError(f"Unexpected provider on {role}: {synth.last_provider}")
        if synth.voice_roles.get("narrator") != "Iapetus":
            raise RuntimeError(f"Unexpected voice on {role}: {synth.voice_roles}")
        with wave.open(str(path), "rb") as wav:
            if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, 24000):
                raise RuntimeError(f"Invalid WAV format: {role}")
            pcm = wav.readframes(wav.getnframes())
            seconds = wav.getnframes() / wav.getframerate()
        info.append({"unit": role, "voice": "Iapetus", "mode": mode,
                     "seconds": round(seconds, 2), "text": spoken})
        wave_segments.append((pcm, 0.25 if role in {"hook", "prayer"} else 0.0))
        if index != len(VOICE_UNITS):
            time.sleep(18)
    target = out / "Nidaa_Iapetus_full_identity_test.wav"
    with wave.open(str(target), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        for segment, gap in wave_segments:
            wav.writeframes(segment)
            if gap > 0:
                wav.writeframes(b"\x00\x00" * round(gap * 24000))
    (out / "review.json").write_text(
        json.dumps({"model": GEMINI38_TTS_MODEL, "status": "generated",
                    "sample_purpose": "voice continuity, no final video",
                    "units": info}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Iapetus full voice sequence success:")
    for item in info:
        print(f"  {item['unit']}: {item['voice']} {item['seconds']}s")

if __name__ == "__main__":
    main()
