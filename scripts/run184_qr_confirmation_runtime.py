from __future__ import annotations

"""Runtime composition for the Run184 QR confirmation closure.

Keep the decision policy in ``run184_qr_confirmation_closure`` and the package/runtime
ownership in ``qr_runtime_bootstrap``.  This module composes them without teaching the
Security policy how to mutate the host environment.
"""

import shutil

from scripts import run184_qr_confirmation_closure as qr
from scripts.qr_runtime_bootstrap import (
    QRRuntimeBootstrapError,
    ensure_qr_confirmation_runtime,
)


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


def install_run184_qr_confirmation_runtime() -> None:
    """Compose pinned mature decoder runtime with the canonical QR policy."""
    global _INSTALLED
    if _INSTALLED:
        return
    current = qr._required_runtime
    if not getattr(current, "_isco_run184_runtime_bootstrap", False):
        _required_runtime_with_bootstrap._isco_run184_runtime_bootstrap = True
        _required_runtime_with_bootstrap._isco_run184_original = current
        qr._required_runtime = _required_runtime_with_bootstrap
    qr.install_run184_qr_confirmation_closure()
    _INSTALLED = True
