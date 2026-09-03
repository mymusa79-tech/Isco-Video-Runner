from __future__ import annotations

"""Production-scoped composition for the Run184 QR confirmation closure.

The existing Media Trust V2 scanner remains authoritative for every non-QR Security
finding and keeps its certified duration-aware 3..8-frame coverage.  During the real
``orchestrator.produce()`` scope only, QR-only heuristic findings are prevented from
terminating that scanner early; after the complete historical scan returns, a separate
mature ZBar + ZXing-C++ confirmation pass decides QR blocking authority.

This preserves all existing OCR/barcode/URL/prompt/text-density coverage while removing
the home-grown QR heuristic as a final authority.  Historical diagnostics and tests see
the exact pre-Run184 behavior outside the production scope.
"""

import re
import shutil
import subprocess
import tempfile
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Literal

import isco_video_agent.orchestrator as orchestrator

from scripts import run184_qr_confirmation_closure as qr
from scripts import security_v1_live_binding as security_binding
from scripts.qr_runtime_bootstrap import (
    QRRuntimeBootstrapError,
    ensure_qr_confirmation_runtime,
)


_RUN184_ACTIVE: ContextVar[bool] = ContextVar("isco_run184_qr_active", default=False)
_INSTALLED = False
_ZXING_221_DECODED_RE = re.compile(r'(?:^|\s)QRCode\s+"', re.IGNORECASE)
_ZXING_221_DETECTED_RE = re.compile(r"(?:^|\s)QRCode(?:\s|$)", re.IGNORECASE)
_ZXING_221_NONE_RE = re.compile(r"(?:^|\s)None(?:\s|$)", re.IGNORECASE)


def _required_runtime_with_bootstrap() -> tuple[str, str, str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    tesseract = shutil.which("tesseract")
    if not ffmpeg or not ffprobe:
        raise qr.QRConfirmationInfrastructureError("ffmpeg_unavailable")
    if not tesseract:
        raise qr.QRConfirmationInfrastructureError("ocr_runtime_unavailable")
    try:
        tools = ensure_qr_confirmation_runtime(allow_install=True)
    except QRRuntimeBootstrapError as exc:
        raise qr.QRConfirmationInfrastructureError(str(exc)) from exc
    return ffmpeg, ffprobe, tools.zbarimg, tools.zxing_reader


def _zxing_qr_status_221(
    frame: Path,
    executable: str,
) -> Literal["none", "decoded", "detected_error"]:
    """Parse the exact ZXingReader 2.2.1 one-line contract shipped by Ubuntu Noble."""
    try:
        completed = subprocess.run(
            [
                executable,
                "-format",
                "QRCode",
                "-errors",
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
        raise qr.QRConfirmationInfrastructureError("qr_confirmation_failed") from exc

    # ZXing-C++ 2.2.1 can return non-zero for a detected symbol with a decode/checksum
    # error.  Therefore classify its structured stdout before interpreting the process
    # status as an infrastructure failure.
    output = completed.stdout or ""
    if _ZXING_221_DECODED_RE.search(output):
        return "decoded"
    if _ZXING_221_DETECTED_RE.search(output):
        return "detected_error"
    if completed.returncode == 0 and _ZXING_221_NONE_RE.search(output):
        return "none"
    raise qr.QRConfirmationInfrastructureError("qr_confirmation_failed")


def _run184_mature_qr_scan(media: str | Path) -> None:
    """Run only mature QR confirmation after the complete historical Security scan."""
    source = Path(media)
    if not source.is_file():
        raise qr._firewall_error("media_missing")
    try:
        ffmpeg, ffprobe, zbar, zxing = _required_runtime_with_bootstrap()
    except qr.QRConfirmationInfrastructureError as exc:
        raise qr._firewall_error(str(exc)) from exc

    video = source.suffix.lower() in security_binding._VIDEO_SUFFIXES
    with tempfile.TemporaryDirectory(prefix="isco-security-qr-v3-") as tmp:
        root = Path(tmp)
        try:
            frames = qr._extract_security_frames(
                source,
                root,
                video=video,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
            )
        except RuntimeError as exc:
            raise qr._firewall_error(str(exc)) from exc

        mature_error_detections = 0
        try:
            for frame in frames:
                if qr._zbar_decodes_qr(frame, zbar):
                    raise qr._firewall_error("qr_code_detected")
                status = _zxing_qr_status_221(frame, zxing)
                if status == "decoded":
                    raise qr._firewall_error("qr_code_detected")
                if status == "detected_error":
                    mature_error_detections += 1
        except qr.QRConfirmationInfrastructureError as exc:
            raise qr._firewall_error(str(exc)) from exc

        if not video and mature_error_detections:
            raise qr._firewall_error("qr_code_detected")
        if video and mature_error_detections >= qr._TEMPORAL_MATURE_DETECTION_QUORUM:
            raise qr._firewall_error("qr_code_detected")


def _qr_only_heuristic_result(scan_result: object) -> bool:
    codes = qr._scan_codes(scan_result)
    return bool(codes) and set(codes) == {"qr_code_detected"}


def install_run184_qr_confirmation_runtime() -> None:
    """Install production-scoped QR authority without replacing certified Security."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Media Trust V2 owns the duration-aware source scanner.  Keep it intact and run it
    # first.  A scoped require() dispatcher merely prevents QR-only heuristic findings
    # from terminating that scanner before later frames can expose non-QR findings.
    prior_require = security_binding.require_normal_vision_safe
    if not getattr(prior_require, "_isco_run184_qr_require_dispatcher", False):
        def require_dispatch(scan_result: object) -> None:
            if _RUN184_ACTIVE.get() and _qr_only_heuristic_result(scan_result):
                print(
                    "Security QR V3 deferred QR-only heuristic finding to mature confirmation; "
                    "all non-QR findings remain under the certified scanner"
                )
                return None
            return prior_require(scan_result)

        require_dispatch._isco_run184_qr_require_dispatcher = True
        require_dispatch._isco_run184_original = prior_require
        security_binding.require_normal_vision_safe = require_dispatch

    prior_scanner: Callable[[str | Path], None] = security_binding._scan_media_before_vision
    if not getattr(prior_scanner, "_isco_run184_qr_dispatcher", False):
        def dispatch(media: str | Path) -> None:
            if not _RUN184_ACTIVE.get():
                return prior_scanner(media)
            # Preserve every historical non-QR Security check and the full 3..8-frame
            # Media Trust coverage before spending any mature QR confirmation work.
            prior_scanner(media)
            return _run184_mature_qr_scan(media)

        dispatch._isco_run184_qr_dispatcher = True
        dispatch._isco_run184_original = prior_scanner
        security_binding._scan_media_before_vision = dispatch

    current_produce: Callable[..., Any] = orchestrator.produce
    if not getattr(current_produce, "_isco_run184_qr_scope", False):
        def scoped_produce(*args: Any, **kwargs: Any):
            token = _RUN184_ACTIVE.set(True)
            try:
                return current_produce(*args, **kwargs)
            finally:
                _RUN184_ACTIVE.reset(token)

        scoped_produce._isco_run184_qr_scope = True
        scoped_produce._isco_run184_original = current_produce
        orchestrator.produce = scoped_produce

    _INSTALLED = True
