from __future__ import annotations

"""Production-scoped composition for the Run184 QR confirmation closure.

The existing Media Trust V2 scanner remains authoritative for every non-QR Security
finding and keeps its certified duration-aware 3..8-frame coverage. During the real
canonical production scopes -- both ``orchestrator.produce()`` and post-core Gold
Cinematic/Short work -- QR-only heuristic findings are prevented from terminating that
scanner early; after the complete historical scan returns, a separate mature ZBar +
ZXing-C++ confirmation pass decides QR blocking authority.

This preserves all existing OCR/barcode/URL/prompt/text-density coverage while removing
the home-grown QR heuristic as a final authority. Historical diagnostics and tests see
the exact pre-Run184 behavior outside a live canonical production scope.
"""

import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

import isco_video_agent.orchestrator as orchestrator

from scripts import run184_qr_confirmation_closure as qr
from scripts import security_v1_live_binding as security_binding
from scripts.qr_runtime_bootstrap import (
    QRRuntimeBootstrapError,
    ensure_qr_confirmation_runtime,
)
from scripts.runtime_phase import canonical_runtime_enabled


_RUN184_ACTIVE: ContextVar[bool] = ContextVar("isco_run184_qr_active", default=False)
_INSTALLED = False
_ZXING_221_DECODED_RE = re.compile(r'(?:^|\s)QRCode\s+"', re.IGNORECASE)
_ZXING_221_DETECTED_RE = re.compile(r"(?:^|\s)QRCode(?:\s|$)", re.IGNORECASE)
_ZXING_221_NONE_RE = re.compile(r"(?:^|\s)None(?:\s|$)", re.IGNORECASE)


@contextmanager
def production_qr_confirmation_scope() -> Iterator[None]:
    """Activate mature QR authority for one live production-owned call scope.

    ContextVar tokens make nested scopes safe: the core produce wrapper and the later
    Gold wrapper can each own their lifetime without leaking authority into historical
    diagnostics, tests, or a later production request. Exceptions always restore the
    exact previous state.
    """
    token = _RUN184_ACTIVE.set(True)
    try:
        yield
    finally:
        _RUN184_ACTIVE.reset(token)


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
    # error. Therefore classify its structured stdout before interpreting the process
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


def _install_post_core_gold_scope() -> None:
    """Keep mature QR authority live for Gold-owned stock selection in real V4 only.

    Run 201 exposed a lifecycle seam: ``orchestrator.produce()`` returned before Gold's
    Short Cinematic Director fetched and preflighted additional stock. The ContextVar
    therefore reset and those candidates fell back to the historical QR heuristic.
    Patch the live run_v3_voice entrypoint alias itself so every Gold wrapper installed
    later nests around this scope instead of bypassing it. Import/tests outside the
    canonical runtime keep historical behavior unchanged.
    """
    if not canonical_runtime_enabled():
        return

    # Lazy import avoids coupling this Media owner to runtime_reliability at module
    # import time. The helper already knows how to patch both package-import mode and
    # the real ``python ../scripts/run_v3_voice.py`` __main__ module.
    from scripts.runtime_reliability import production_entrypoint_modules

    for production in production_entrypoint_modules():
        current_gold = getattr(production, "run_gold_enforce_phase4", None)
        if not callable(current_gold):
            raise RuntimeError("Run184 QR runtime could not locate Gold production entrypoint")
        if getattr(current_gold, "_isco_run184_qr_gold_scope", False):
            continue

        def make_gold_wrapper(current: Callable[..., Any]):
            def scoped_gold(*args: Any, **kwargs: Any):
                with production_qr_confirmation_scope():
                    return current(*args, **kwargs)

            scoped_gold._isco_run184_qr_gold_scope = True
            scoped_gold._isco_run184_qr_gold_original = current
            return scoped_gold

        setattr(production, "run_gold_enforce_phase4", make_gold_wrapper(current_gold))

    print(
        "Security QR V3 post-core Gold scope installed: mature ZBar/ZXing authority "
        "covers Short Cinematic stock preflight"
    )


def install_run184_qr_confirmation_runtime() -> None:
    """Install production-scoped QR authority without replacing certified Security."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Media Trust V2 owns the duration-aware source scanner. Keep it intact and run it
    # first. A scoped require() dispatcher merely prevents QR-only heuristic findings
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
            with production_qr_confirmation_scope():
                return current_produce(*args, **kwargs)

        scoped_produce._isco_run184_qr_scope = True
        scoped_produce._isco_run184_original = current_produce
        orchestrator.produce = scoped_produce

    # Gold/Short Cinematic is a second production-owned media scope after core produce.
    # Install this before the later release-transaction wrapper so release journaling
    # composes outside it without changing QR/security semantics.
    _install_post_core_gold_scope()

    _INSTALLED = True
