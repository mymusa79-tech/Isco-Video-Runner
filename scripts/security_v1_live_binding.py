from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.brief_approval_binding import verify_brief_approval
from isco_video_agent.model_output_schemas import (
    validate_alternate_visual_query,
    validate_visual_query,
)
from isco_video_agent.multimodal_firewall import (
    MultimodalInjectionFirewall,
    require_normal_vision_safe,
)
from isco_video_agent.research_quarantine import ResearchQuarantineExtractor


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})
_INSTALLED = False


def _expected_approved_brief_hash() -> str:
    value = (os.environ.get("ISCO_APPROVED_BRIEF_SHA256") or "").strip().lower()
    if not _HASH_RE.fullmatch(value):
        raise RuntimeError("Security V1 requires a valid ISCO_APPROVED_BRIEF_SHA256")
    return value


def _verify_production_brief() -> str:
    brief = orchestrator.load_approved_brief(required=True)
    if not isinstance(brief, dict):
        raise RuntimeError("Security V1 approved brief loader returned no object")
    return verify_brief_approval(brief, _expected_approved_brief_hash())


def _quarantined_market_signals(signals: dict) -> list[dict[str, Any]]:
    """Expose only typed, validated external facts to the planner/model sink."""
    packet = ResearchQuarantineExtractor().extract(signals)
    payload = packet.planner_payload()
    if packet.rejected:
        print(f"Security V1 research quarantine rejected {len(packet.rejected)} raw records")
    return payload


def _validated_visual_query(value: object) -> str:
    return validate_visual_query(value).as_downstream_data()


def _validated_alternate_query(value: object) -> str:
    return validate_alternate_visual_query(value).as_downstream_data()


def _production_ocr(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise RuntimeError("local_ocr_unavailable")
    completed = subprocess.run(
        [tesseract, str(path), "stdout", "--psm", "6", "-l", "eng+ara"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise RuntimeError("local_ocr_failed")
    return completed.stdout


def _scan_media_before_vision(media: str | Path) -> None:
    """Sample local media to PGM frames and fail closed before any cloud Vision call.

    Images yield one frame. Videos yield up to the first three one-second samples.
    No media content, OCR text, or detection detail is promoted into a model prompt.
    """
    source = Path(media)
    if not source.is_file():
        raise RuntimeError("multimodal_injection_firewall_block:media_missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("multimodal_injection_firewall_block:ffmpeg_unavailable")

    with tempfile.TemporaryDirectory(prefix="isco-security-frame-") as tmp:
        root = Path(tmp)
        pattern = root / "frame-%02d.pgm"
        video = source.suffix.lower() in _VIDEO_SUFFIXES
        filters = "scale=640:-2:force_original_aspect_ratio=decrease,format=gray"
        if video:
            filters = "fps=1," + filters
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-frames:v",
            "3" if video else "1",
            "-vf",
            filters,
            str(pattern),
        ]
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        frames = sorted(root.glob("frame-*.pgm"))
        if completed.returncode != 0 or not frames:
            raise RuntimeError("multimodal_injection_firewall_block:frame_extract_failed")
        firewall = MultimodalInjectionFirewall(ocr_backend=_production_ocr)
        for frame in frames:
            require_normal_vision_safe(firewall.scan_frame(frame))


def _wrap_search(original: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(original, "_isco_security_v1_search", False):
        return original

    def wrapped(*args, **kwargs):
        if len(args) >= 2:
            safe = _validated_visual_query(args[1])
            args = (args[0], safe, *args[2:])
        elif "query" in kwargs:
            kwargs = dict(kwargs)
            kwargs["query"] = _validated_visual_query(kwargs["query"])
        else:
            raise RuntimeError("Security V1 could not locate stock-search query")
        return original(*args, **kwargs)

    wrapped._isco_security_v1_search = True
    wrapped._isco_security_v1_original = original
    return wrapped


def _wrap_vision_audit(original: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(original, "_isco_security_v1_vision", False):
        return original

    def wrapped(*args, **kwargs):
        preview = args[1] if len(args) >= 2 else kwargs.get("preview")
        if preview is None:
            raise RuntimeError("Security V1 could not locate vision preview")
        _scan_media_before_vision(preview)
        return original(*args, **kwargs)

    wrapped._isco_security_v1_vision = True
    wrapped._isco_security_v1_original = original
    return wrapped


def install_security_v1_live_binding() -> None:
    """Install Security V1 at real Production V4 trust boundaries without adding provider calls."""
    global _INSTALLED
    if _INSTALLED:
        return

    current_produce = orchestrator.produce
    if not getattr(current_produce, "_isco_security_v1_produce", False):
        def secured_produce(*args, **kwargs):
            # Runner uses this binding only for real V4. Verify the exact approved brief
            # before the core can research, plan, call providers, or render media.
            _verify_production_brief()
            return current_produce(*args, **kwargs)

        secured_produce._isco_security_v1_produce = True
        secured_produce._isco_security_v1_original = current_produce
        orchestrator.produce = secured_produce

    # External research is narrowed to deterministic typed facts before Planner/factuality prompts.
    orchestrator.compact_signals = _quarantined_market_signals

    # All stock-provider queries, including model-generated alternate queries, cross a strict schema gate.
    orchestrator.pexels_search_videos = _wrap_search(orchestrator.pexels_search_videos)
    orchestrator.pixabay_provider.search_videos = _wrap_search(orchestrator.pixabay_provider.search_videos)
    thumbnail.search_photos = _wrap_search(thumbnail.search_photos)
    thumbnail.pixabay_provider.search_photos = _wrap_search(thumbnail.pixabay_provider.search_photos)

    current_alt = orchestrator.suggest_alternate_visual_query
    if not getattr(current_alt, "_isco_security_v1_alt_query", False):
        def secured_alt_query(*args, **kwargs):
            return _validated_alternate_query(current_alt(*args, **kwargs))

        secured_alt_query._isco_security_v1_alt_query = True
        secured_alt_query._isco_security_v1_original = current_alt
        orchestrator.suggest_alternate_visual_query = secured_alt_query

    # Local multimodal firewall runs before any selected stock preview reaches Gemini Vision.
    orchestrator.audit_video_preview = _wrap_vision_audit(orchestrator.audit_video_preview)
    thumbnail.audit_image_preview = _wrap_vision_audit(thumbnail.audit_image_preview)

    _INSTALLED = True
