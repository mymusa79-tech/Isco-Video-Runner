from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


CURRENT_MEDIA_TRUST_SUBPROCESS_TIMEOUT_SECONDS = 20.0


def _timed(command: list[str]) -> tuple[float, subprocess.CompletedProcess[str]]:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=CURRENT_MEDIA_TRUST_SUBPROCESS_TIMEOUT_SECONDS,
    )
    return time.monotonic() - started, completed


def main() -> int:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg/ffprobe required for Media Trust timeout measurement")

    measurements: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="isco-media-trust-timeout-measure-") as tmp:
        root = Path(tmp)
        source = root / "representative-1080p.mp4"

        # Synthetic, deterministic 1080p H.264 asset with a long timeline. This is not
        # a claim about every stock file; it is a repeatable CI measurement of the exact
        # ffprobe and random-access frame extraction operations protected by the 20s
        # Media Trust subprocess timeout. Run127 will provide real-production evidence.
        create = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=1920x1080:r=30:d=180",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "34",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        if create.returncode != 0 or not source.is_file():
            raise SystemExit("failed to create deterministic Media Trust measurement asset")

        elapsed, probe = _timed(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ]
        )
        if probe.returncode != 0:
            raise SystemExit("measurement ffprobe failed")
        measurements.append({"operation": "ffprobe_duration", "elapsed_seconds": elapsed})

        for index, timestamp in enumerate((0.0, 45.0, 90.0, 179.0), 1):
            frame = root / f"frame-{index:02d}.pgm"
            elapsed, extract = _timed(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2:force_original_aspect_ratio=decrease,format=gray",
                    str(frame),
                ]
            )
            if extract.returncode != 0 or not frame.is_file() or frame.stat().st_size <= 0:
                raise SystemExit(f"measurement frame extraction failed at {timestamp}s")
            measurements.append(
                {
                    "operation": "frame_extract",
                    "timestamp_seconds": timestamp,
                    "elapsed_seconds": elapsed,
                }
            )

    maximum = max(float(item["elapsed_seconds"]) for item in measurements)
    ratio = CURRENT_MEDIA_TRUST_SUBPROCESS_TIMEOUT_SECONDS / max(maximum, 0.001)
    if maximum <= 5.0:
        assessment = "comfortable_headroom"
    elif maximum <= 10.0:
        assessment = "adequate_headroom"
    else:
        assessment = "narrow_headroom_needs_real_asset_followup"

    payload = {
        "schema_version": 1,
        "timeout_seconds": CURRENT_MEDIA_TRUST_SUBPROCESS_TIMEOUT_SECONDS,
        "max_elapsed_seconds": round(maximum, 4),
        "timeout_to_observed_ratio": round(ratio, 2),
        "assessment": assessment,
        "measurements": [
            {
                **item,
                "elapsed_seconds": round(float(item["elapsed_seconds"]), 4),
            }
            for item in measurements
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
