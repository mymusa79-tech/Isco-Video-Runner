from __future__ import annotations

"""Production-scoped composition for the Run184 QR confirmation closure.

The policy module is intentionally pure. This owner activates it only while the real
``orchestrator.produce()`` call is executing, so historical diagnostics/unit contracts
continue to observe the exact scanner installed before this closure. The same scope also
owns the exact Ubuntu Noble ZXing-C++ 2.2.1 CLI dialect; no process-global compatibility
patch is allowed to leak into historical tests or another run.
"""

import re
import shutil
import subprocess
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
_ZXING_221_DECODED_RE = re.compile(r"(?:^|\s)QRCode\s+\"", re.IGNORECASE)
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
    """Parse the exact ZXingReader 2.2.1 one-line contract shipped by Ubuntu Noble.

    v2.2.1 uses ``-format`` (singular) and has no ``-single`` option. With ``-errors``
    its process return code can legitimately be non-zero when a QR symbol was detected
    but failed checksum/format decoding, so stdout semantics must be interpreted before
    treating a non-zero status as an infrastructure failure.
    """
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

    output = completed.stdout or ""
    if _ZXING_221_DECODED_RE.search(output):
        return "decoded"
    if _ZXING_221_DETECTED_RE.search(output):
        return "detected_error"
    if completed.returncode == 0 and _ZXING_221_NONE_RE.search(output):
        return "none"
    raise qr.QRConfirmationInfrastructureError("qr_confirmation_failed")


def _run184_scan(media: str | Path) -> None:
    """Execute QR V3 without mutating historical scanner/runtime ownership."""
    source = Path(media)
    if not source.is_file():
        raise qr._firewall_error("media_missing")
    try:
        ffmpeg, ffprobe, zbar, zxing = _required_runtime_with_bootstrap()
    except qr.QRConfirmationInfrastructureError as exc:
        raise qr._firewall_error(str(exc)) from exc

    video = source.suffix.lower() in security_binding._VIDEO_SUFFIXES
    import tempfile

    with tempfile.TemporaryDirectory(prefix="isco-security-v3-") as tmp:
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

        firewall = qr.MultimodalInjectionFirewall(ocr_backend=security_binding._production_ocr)
        try:
            qr._evaluate_frames(
                frames,
                video=video,
                zbar=zbar,
                zxing=zxing,
                firewall=firewall,
            )
        except qr.QRConfirmationInfrastructureError as exc:
            raise qr._firewall_error(str(exc)) from exc


def install_run184_qr_confirmation_runtime() -> None:
    """Install dispatchers + produce scope; outside Production behavior is unchanged."""
    global _INSTALLED
    if _INSTALLED:
        return

    prior_scanner: Callable[[str | Path], None] = security_binding._scan_media_before_vision
    if not getattr(prior_scanner, "_isco_run184_qr_dispatcher", False):
        def dispatch(media: str | Path) -> None:
            if _RUN184_ACTIVE.get():
                return _run184_scan(media)
            return prior_scanner(media)

        dispatch._isco_run184_qr_dispatcher = True
        dispatch._isco_run184_original = prior_scanner
        security_binding._scan_media_before_vision = dispatch

    prior_zxing = qr._zxing_qr_status
    if not getattr(prior_zxing, "_isco_run184_zxing_dispatcher", False):
        def zxing_dispatch(frame: Path, executable: str):
            if _RUN184_ACTIVE.get():
                return _zxing_qr_status_221(frame, executable)
            return prior_zxing(frame, executable)

        zxing_dispatch._isco_run184_zxing_dispatcher = True
        zxing_dispatch._isco_run184_original = prior_zxing
        qr._zxing_qr_status = zxing_dispatch

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
