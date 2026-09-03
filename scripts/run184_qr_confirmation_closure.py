from __future__ import annotations

"""Run #184 QR confirmation closure.

Run #184 proved the Visual Retrieval family was active, but Security V1 quarantined
18/21 inspected stock candidates as ``qr_code_detected`` before cloud Vision. The
historical Engine QR detector is intentionally only a cheap 1:1:3:1:1 suspicion stage;
PR #482 added a dependency-free geometry check, but Run #184 demonstrated that a
home-grown geometry heuristic still must not be the sole final authority.

This closure keeps every existing OCR / barcode / URL / prompt / text-density finding
unchanged while giving QR-only suspicion a production-grade confirmation cascade:

1. sample the *whole* video at 12%, 50% and 88% instead of only the first 3 seconds;
2. run the existing multimodal firewall/OCR on every sampled frame;
3. independently scan every frame with two mature Ubuntu-packaged QR engines:
   ZBar (zbarimg) and ZXing-C++ (ZXingReader);
4. any valid QR decode from either mature engine is an immediate hard block;
5. an undecodable QR found by ZXing's mature detector receives blocking authority for
   video only with temporal confirmation on at least two distributed frames;
6. a still image has no temporal dimension, so one ZXing QR detection (including a
   decode/checksum error) is sufficient to retain the QR block;
7. Engine/home-grown geometry remains diagnostic evidence only and can never create a
   QR block by itself;
8. missing/broken QR confirmation infrastructure remains a production hard failure.

No provider call, retry, Vision budget, Quality threshold, Cultural/Islamic gate or
semantic relevance threshold is changed.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable, Literal

from isco_video_agent.multimodal_firewall import MultimodalInjectionFirewall

from scripts import security_v1_live_binding as security_binding
from scripts.qr_geometry_confirmation import confirm_qr_finder_geometry


CONTRACT_ID = "run184-qr-confirmation-closure-v1"
CONTRACT_VERSION = 1
_VIDEO_SAMPLE_FRACTIONS = (0.12, 0.50, 0.88)
_TEMPORAL_MATURE_DETECTION_QUORUM = 2
_ZXING_QR_RE = re.compile(r"(?:^|\s)QR\s*Code(?:\s|$)", re.IGNORECASE)
_ZXING_VALID_QR_RE = re.compile(r"(?:^|\s)QR\s*Code\s+\"", re.IGNORECASE)
_INSTALLED = False


class QRConfirmationInfrastructureError(RuntimeError):
    """A mandatory local QR confirmation dependency failed or disappeared."""


def _firewall_error(code: str) -> RuntimeError:
    prefix = security_binding._FIREWALL_BLOCK_PREFIX
    return RuntimeError(f"{prefix}{code}")


def _required_runtime() -> tuple[str, str, str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    zbar = shutil.which("zbarimg")
    zxing = shutil.which("ZXingReader")
    if not ffmpeg or not ffprobe:
        raise QRConfirmationInfrastructureError("ffmpeg_unavailable")
    if not zbar or not zxing:
        raise QRConfirmationInfrastructureError("qr_confirmation_runtime_unavailable")
    if not shutil.which("tesseract"):
        raise QRConfirmationInfrastructureError("ocr_runtime_unavailable")
    return ffmpeg, ffprobe, zbar, zxing


def _video_sample_times(duration_seconds: float) -> tuple[float, ...]:
    duration = float(duration_seconds)
    if duration <= 0:
        raise ValueError("non_positive_duration")
    last_safe = max(0.0, duration - 0.05)
    times: list[float] = []
    for fraction in _VIDEO_SAMPLE_FRACTIONS:
        value = min(last_safe, max(0.0, duration * fraction))
        if not any(abs(value - existing) < 0.025 for existing in times):
            times.append(value)
    if not times:
        times.append(0.0)
    return tuple(times)


def _probe_duration(source: Path, ffprobe: str) -> float:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(source),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=12,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("frame_extract_timeout") from exc
    if completed.returncode != 0:
        raise RuntimeError("frame_extract_failed")
    try:
        duration = float(completed.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("frame_extract_failed") from exc
    if duration <= 0:
        raise RuntimeError("frame_extract_failed")
    return duration


def _extract_one_frame(
    source: Path,
    destination: Path,
    *,
    ffmpeg: str,
    timestamp: float | None,
) -> None:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if timestamp is not None:
        # Input seeking is intentional here: stock clips can be long, while the local
        # security sampler only needs a representative frame near each distributed
        # timestamp. This avoids decoding the clip from frame zero three separate times.
        command.extend(["-ss", f"{timestamp:.3f}"])
    command.extend(
        [
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2:force_original_aspect_ratio=decrease,format=gray",
            str(destination),
        ]
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("frame_extract_timeout") from exc
    if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
        raise RuntimeError("frame_extract_failed")


def _extract_security_frames(
    source: Path,
    root: Path,
    *,
    video: bool,
    ffmpeg: str,
    ffprobe: str,
) -> tuple[Path, ...]:
    if not video:
        frame = root / "frame-01.pgm"
        _extract_one_frame(source, frame, ffmpeg=ffmpeg, timestamp=None)
        return (frame,)

    duration = _probe_duration(source, ffprobe)
    frames: list[Path] = []
    for index, timestamp in enumerate(_video_sample_times(duration), start=1):
        frame = root / f"frame-{index:02d}.pgm"
        _extract_one_frame(source, frame, ffmpeg=ffmpeg, timestamp=timestamp)
        frames.append(frame)
    if not frames:
        raise RuntimeError("frame_extract_failed")
    return tuple(frames)


def _zbar_decodes_qr(frame: Path, executable: str) -> bool:
    try:
        completed = subprocess.run(
            [
                executable,
                "--quiet",
                "--raw",
                "--nodisplay",
                "-Sdisable",
                "-Sqrcode.enable",
                "-Stest-inverted=1",
                str(frame),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired as exc:
        raise QRConfirmationInfrastructureError("qr_confirmation_failed") from exc
    # zbarimg documents: 0=decoded, 4=no barcode, 1/2/3 are processing/fatal/user
    # failures. Only the explicit no-barcode status may safely mean "not confirmed".
    if completed.returncode == 0:
        return bool((completed.stdout or "").strip())
    if completed.returncode == 4:
        return False
    raise QRConfirmationInfrastructureError("qr_confirmation_failed")


def _zxing_qr_status(
    frame: Path,
    executable: str,
) -> Literal["none", "decoded", "detected_error"]:
    try:
        completed = subprocess.run(
            [
                executable,
                "-formats",
                "QRCode",
                "-errors",
                "-single",
                "-1",
                str(frame),
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    except subprocess.TimeoutExpired as exc:
        raise QRConfirmationInfrastructureError("qr_confirmation_failed") from exc

    # ZXingReader 2.2.x prints one line per file/result. A clean no-code scan uses
    # format None and returns zero. With -errors it can also report a QR symbol whose
    # payload/checksum is not decodable; that is mature detection evidence but not a
    # valid decode. Any non-zero process status is treated as infrastructure failure.
    if completed.returncode != 0:
        raise QRConfirmationInfrastructureError("qr_confirmation_failed")
    output = completed.stdout or ""
    if not _ZXING_QR_RE.search(output):
        return "none"
    if _ZXING_VALID_QR_RE.search(output):
        return "decoded"
    return "detected_error"


def _scan_codes(scan_result: object) -> tuple[str, ...]:
    detections = tuple(getattr(scan_result, "detections", ()) or ())
    codes = tuple(str(getattr(item, "code", "")).strip() for item in detections)
    return tuple(code for code in codes if code)


def _evaluate_frames(
    frames: Iterable[Path],
    *,
    video: bool,
    zbar: str,
    zxing: str,
    firewall: MultimodalInjectionFirewall,
) -> None:
    engine_qr_suspicions = 0
    geometry_observations = 0
    mature_error_detections = 0

    for frame in tuple(frames):
        scan_result = firewall.scan_frame(frame)
        codes = _scan_codes(scan_result)
        non_qr_codes = tuple(code for code in codes if code != "qr_code_detected")
        # Every non-QR finding keeps exactly its historical authority. Never let QR
        # adjudication remove barcode/text/URL/prompt/command findings.
        if non_qr_codes:
            raise _firewall_error(",".join(non_qr_codes))

        # Mature engines scan every distributed frame even when the cheap Engine
        # heuristic missed QR. This also closes the historical first-three-seconds gap.
        if _zbar_decodes_qr(frame, zbar):
            raise _firewall_error("qr_code_detected")
        zxing_status = _zxing_qr_status(frame, zxing)
        if zxing_status == "decoded":
            raise _firewall_error("qr_code_detected")
        if zxing_status == "detected_error":
            mature_error_detections += 1

        if "qr_code_detected" in codes:
            engine_qr_suspicions += 1
            # Historical geometry is retained strictly as diagnostics/regression
            # evidence. It no longer has independent blocking authority.
            if confirm_qr_finder_geometry(frame):
                geometry_observations += 1

    if not video and mature_error_detections:
        raise _firewall_error("qr_code_detected")
    if video and mature_error_detections >= _TEMPORAL_MATURE_DETECTION_QUORUM:
        raise _firewall_error("qr_code_detected")

    if engine_qr_suspicions or mature_error_detections:
        print(
            "Security QR V3: no blocking mature QR confirmation "
            f"(engine_suspicions={engine_qr_suspicions}, "
            f"legacy_geometry={geometry_observations}, "
            f"mature_error_detections={mature_error_detections}); "
            "retaining all other Security findings"
        )


def _scan_media_before_vision_v3(media: str | Path) -> None:
    source = Path(media)
    if not source.is_file():
        raise _firewall_error("media_missing")
    try:
        ffmpeg, ffprobe, zbar, zxing = _required_runtime()
    except QRConfirmationInfrastructureError as exc:
        raise _firewall_error(str(exc)) from exc

    video = source.suffix.lower() in security_binding._VIDEO_SUFFIXES
    with tempfile.TemporaryDirectory(prefix="isco-security-v3-") as tmp:
        root = Path(tmp)
        try:
            frames = _extract_security_frames(
                source,
                root,
                video=video,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        except RuntimeError as exc:
            raise _firewall_error(str(exc)) from exc
        firewall = MultimodalInjectionFirewall(ocr_backend=security_binding._production_ocr)
        try:
            _evaluate_frames(
                frames,
                video=video,
                zbar=zbar,
                zxing=zxing,
                firewall=firewall,
            )
        except QRConfirmationInfrastructureError as exc:
            raise _firewall_error(str(exc)) from exc


def install_run184_qr_confirmation_closure() -> None:
    """Replace only Security V1's local media scanner at the canonical Media seam."""
    global _INSTALLED
    if _INSTALLED:
        return
    current = security_binding._scan_media_before_vision
    if not getattr(current, "_isco_run184_qr_confirmation", False):
        _scan_media_before_vision_v3._isco_run184_qr_confirmation = True
        _scan_media_before_vision_v3._isco_run184_original = current
        security_binding._scan_media_before_vision = _scan_media_before_vision_v3

    # Make infrastructure classification explicit. Even without this addition unknown
    # codes fail closed; declaring ownership prevents a future local-quarantine list from
    # accidentally converting confirmation-runtime failure into a candidate-only block.
    security_binding._STOCK_INFRASTRUCTURE_HARD_FAIL_CODES = frozenset(
        set(security_binding._STOCK_INFRASTRUCTURE_HARD_FAIL_CODES)
        | {"qr_confirmation_runtime_unavailable", "qr_confirmation_failed"}
    )
    _INSTALLED = True
