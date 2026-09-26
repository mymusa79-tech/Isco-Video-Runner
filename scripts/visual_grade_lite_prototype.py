from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


PROFILES = {
    "warm_rise": (
        "eq=contrast=1.05:saturation=1.06:brightness=0.01,"
        "colorbalance=rs=0.03:gs=0.01:bs=-0.02"
    ),
    "clean_focus": (
        "eq=contrast=1.04:saturation=0.98:brightness=0.005,"
        "colorbalance=rs=0.01:gs=0.00:bs=0.00"
    ),
    "soft_cinematic": (
        "eq=contrast=1.03:saturation=0.94:brightness=-0.005,"
        "colorbalance=rs=0.02:gs=0.00:bs=-0.015"
    ),
}


def apply_profile(source: Path, output: Path, *, profile: str) -> Path:
    if profile not in PROFILES:
        raise ValueError(f"unsupported visual grade profile: {profile}")
    source = Path(source)
    output = Path(output)
    if not source.is_file():
        raise RuntimeError("visual_grade_source_missing")
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            PROFILES[profile],
            "-frames:v",
            "1",
            str(output),
        ],
        check=True,
        timeout=120,
    )
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("visual_grade_output_missing")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Local zero-provider visual-grade prototype")
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES), required=True)
    args = parser.parse_args()
    print(apply_profile(args.source, args.output, profile=args.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
