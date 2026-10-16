#!/usr/bin/env python3
"""Render short audition mixes: each dialogue-bed track under a real narration excerpt.

Uses the production level rules (podcast: -24 dB under narration), the production
track allowlist and verified CC0 downloads. No provider calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build(run_dir: Path, out_dir: Path, *, start: float, duration: float, fmt: str) -> list[dict]:
    from clean_v2 import music_library, short_audio_polish as polish

    out_dir.mkdir(parents=True, exist_ok=True)
    narration = out_dir / "voice.wav"
    polish._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", str(start),
                 "-t", str(duration), "-i", str(run_dir / "narration-mastered.wav"),
                 "-c:a", "pcm_s16le", str(narration)])
    voice_mean = polish._measure_mean_db(narration)
    target, low, high = polish.MUSIC_LEVELS_REL_DB[fmt]
    report = music_library.ensure_music_library(
        allow_download=True, track_ids=sorted(music_library.DIALOGUE_BED_TRACKS))
    results = []
    polish._run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(narration),
                 "-c:a", "libmp3lame", "-q:a", "3", str(out_dir / "0-voice-only.mp3")])
    for index, track in enumerate(sorted(report["ready"], key=lambda t: t["id"]), 1):
        bed = out_dir / f"bed-{track['id']}.wav"
        level = polish._normalize_relative(
            src=Path(track["path"]), dest=bed, narration_mean_db=voice_mean,
            target_relative_db=target, minimum_relative_db=low, maximum_relative_db=high)
        fade_out = max(duration - 3.0, 0.0)
        name = f"{index}-{track['id']}.mp3"
        polish._run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(narration), "-ss", "20", "-t", str(duration), "-i", str(bed),
            "-filter_complex",
            f"[1:a]afade=t=in:st=0:d=2,afade=t=out:st={fade_out}:d=3[m];"
            "[0:a][m]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.84[mix]",
            "-map", "[mix]", "-c:a", "libmp3lame", "-q:a", "3", str(out_dir / name)])
        bed.unlink(missing_ok=True)
        results.append({"file": name, "id": track["id"], "title": track.get("title"),
                        "relative_to_voice_db": round(level["relative_to_narration_db"], 1)})
    narration.unlink(missing_ok=True)
    (out_dir / "audition.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    missing = report.get("unavailable") or []
    if missing:
        print("unavailable:", missing)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", type=float, default=70.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--format", default="podcast", choices=("short", "film", "podcast"))
    args = parser.parse_args()
    for row in build(Path(args.run_dir), Path(args.out), start=args.start, duration=args.duration, fmt=args.format):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
