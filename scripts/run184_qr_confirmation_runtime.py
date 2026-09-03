from __future__ import annotations

"""Production-scoped composition for the Run184 QR confirmation closure.

The policy module is intentionally pure.  This owner activates it only while the real
``orchestrator.produce()`` call is executing, so historical diagnostics/unit contracts
continue to observe the exact scanner installed before this closure.  This mirrors the
run-scoped ownership used by the mature Vision/provider-health closures and prevents a
single Python process from leaking Run184 behavior into later tests or another run.
"""

import shutil
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator

from scripts import run184_qr_confirmation_closure as qr
from scripts import security_v1_live_binding as security_binding
from scripts.qr_runtime_bootstrap import (
    QRRuntimeBootstrapError,
    ensure_qr_confirmation_runtime,
)


_RUN184_ACTIVE: ContextVar[bool] = ContextVar("isco_run184_qr_active", default=False)
_INSTALLED = False


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


def _run184_scan(media: str | Path) -> None:
    """Execute QR V3 without mutating policy-module runtime ownership."""
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
    """Install one dispatcher + produce scope; outside Production behavior is unchanged."""
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
