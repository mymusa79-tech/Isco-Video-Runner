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
    ModelOutputSchemaError,
    VISUAL_QUERY_MAX_LENGTH,
    validate_alternate_visual_query,
    validate_cross_provider_text,
    validate_visual_query,
)
from isco_video_agent.multimodal_firewall import MultimodalInjectionFirewall
from isco_video_agent.research_quarantine import ResearchQuarantineExtractor
from isco_video_agent.stock_media_preflight import install_stock_media_preflight

from scripts.qr_geometry_confirmation import confirm_qr_finder_geometry


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})
_PLAIN_STOCK_QUERY_RE = re.compile(r"^[A-Za-z0-9]+(?:[ '-][A-Za-z0-9]+)*$")
_REPEATED_STOCK_SEPARATOR_RE = re.compile(r"(?: {2,}|--|''| -|- | '|' )")
_FIREWALL_BLOCK_PREFIX = "multimodal_injection_firewall_block:"
_STOCK_CANDIDATE_LOCAL_CODES = frozenset(
    {
        # Candidate media availability / decode failures.
        "media_missing",
        "frame_extract_failed",
        "frame_extract_timeout",
        "frame_unreadable",
        # Fail-closed visual security findings. These are never allowed through;
        # the candidate is quarantined and the bounded selector tries another asset.
        "qr_code_detected",
        "barcode_detected",
        "high_text_density",
        "url_detected",
        "role_marker_detected",
        "prompt_like_text_detected",
        "command_like_text_detected",
        # OCR could not safely inspect this candidate. The runtime itself is
        # preflighted separately, so this remains a candidate-local hard rejection.
        "local_ocr_unavailable",
    }
)
_STOCK_INFRASTRUCTURE_HARD_FAIL_CODES = frozenset(
    {
        "ffmpeg_unavailable",
        "ocr_runtime_unavailable",
    }
)
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


def _normalized_stock_query(value: object, *, alternate: bool = False) -> str:
    """Normalize only the safe-overlong stock-query failure class.

    Run #106 showed that a semantically valid plain-English stock query can exceed the
    strict 80-character model-output contract before it reaches Pexels/Pixabay. The
    security boundary remains authoritative: every query that already validates is
    returned byte-for-byte unchanged, and every failure other than `visual_query_too_long`
    remains a hard failure.

    For the one recoverable length case, validate the *entire original value* through
    the cross-provider injection firewall first, then additionally require the full
    value to be ASCII plain-search syntax. Only after those checks do we shorten at a
    word boundary to the existing 80-character ceiling and re-run the original visual
    query validator. This prevents a malicious/non-English suffix from being hidden by
    truncation while avoiding a needless Production failure for a safe verbose query.
    """
    validator = validate_alternate_visual_query if alternate else validate_visual_query
    try:
        return validator(value).as_downstream_data()
    except ModelOutputSchemaError as exc:
        if str(exc) != "visual_query_too_long":
            raise

    # Full-value security validation MUST happen before shortening. This retains
    # fail-closed behavior for prompt injection, URLs, role markers, shell syntax,
    # structured markup, newlines/control chars and >240-char cross-provider text.
    full = validate_cross_provider_text(value).as_downstream_data()
    if not full.isascii():
        raise ModelOutputSchemaError("visual_query_non_english_or_non_ascii_rejected")
    if not _PLAIN_STOCK_QUERY_RE.fullmatch(full):
        raise ModelOutputSchemaError("visual_query_not_plain_english_search_terms")
    if _REPEATED_STOCK_SEPARATOR_RE.search(full):
        raise ModelOutputSchemaError("visual_query_malformed_separators")

    words = full.split()
    kept: list[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if len(candidate) > VISUAL_QUERY_MAX_LENGTH:
            break
        kept.append(word)

    shortened = " ".join(kept)
    if not shortened:
        raise ModelOutputSchemaError("visual_query_too_long")
    normalized = validator(shortened).as_downstream_data()
    print(
        "Security V1 normalized safe overlong stock query: "
        f"{len(full)} -> {len(normalized)} chars"
    )
    return normalized


def _normalized_optional_alternate_query(value: object) -> str:
    """Preserve Engine's explicit NO_ALTERNATE sentinel without weakening schemas.

    The shared Engine selector documents an empty string as the bounded recovery
    sentinel meaning "there is no safe/useful alternate query". Only an actual string
    that is empty after trimming receives this treatment. None, mappings, malformed
    text, prompt injection, non-English text and every non-empty model output still pass
    through the exact existing alternate-query schema/security boundary and fail closed
    when invalid.
    """
    if isinstance(value, str) and not value.strip():
        return ""
    return _normalized_stock_query(value, alternate=True)


def _production_ocr(path: Path) -> str:
    tesseract = shutil.which("tesseract")
    if not tesseract:
        raise RuntimeError("ocr_runtime_unavailable")
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


def _effective_firewall_block_codes(scan_result: object, frame: Path) -> tuple[str, ...]:
    """Apply QR confirmation while preserving every non-QR firewall finding.

    Engine's QR detector remains the cheap suspicion stage. A QR suspicion gets
    blocking authority only after independent two-axis/geometric confirmation. This is
    not a media approval path: barcode/text/prompt/URL/OCR and future detections are
    unchanged, and confirmation errors themselves retain the QR block (fail closed).
    """
    detections = tuple(getattr(scan_result, "detections", ()) or ())
    codes = tuple(str(getattr(detection, "code", "")).strip() for detection in detections)
    codes = tuple(code for code in codes if code)
    if "qr_code_detected" not in codes:
        return codes
    if confirm_qr_finder_geometry(frame):
        return codes
    print("Security V1 QR suspicion not geometrically confirmed; retaining all other findings")
    return tuple(code for code in codes if code != "qr_code_detected")


def _scan_media_before_vision(media: str | Path) -> None:
    """Sample local media to PGM frames and fail closed before any cloud Vision call.

    Images yield one frame. Videos yield up to the first three one-second samples.
    No media content, OCR text, or detection detail is promoted into a model prompt.

    Runtime dependencies are classified separately from candidate-local failures so
    missing security infrastructure still stops Production, while one bad/untrusted
    stock asset can be quarantined by the bounded selector without weakening checks.
    """
    source = Path(media)
    if not source.is_file():
        raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}media_missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}ffmpeg_unavailable")
    if not shutil.which("tesseract"):
        raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}ocr_runtime_unavailable")

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
        try:
            completed = subprocess.run(
                command,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}frame_extract_timeout") from exc
        frames = sorted(root.glob("frame-*.pgm"))
        if completed.returncode != 0 or not frames:
            raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}frame_extract_failed")
        firewall = MultimodalInjectionFirewall(ocr_backend=_production_ocr)
        for frame in frames:
            scan_result = firewall.scan_frame(frame)
            if getattr(scan_result, "safe_for_normal_vision", False):
                continue
            codes = _effective_firewall_block_codes(scan_result, frame)
            if codes:
                raise RuntimeError(f"{_FIREWALL_BLOCK_PREFIX}{','.join(codes)}")
            # The only possible path here is an unconfirmed QR-only suspicion. No
            # positive safety claim is manufactured; this frame simply has no retained
            # Security V1 finding after the independent QR confirmation stage.


def _wrap_search(original: Callable[..., Any]) -> Callable[..., Any]:
    if getattr(original, "_isco_security_v1_search", False):
        return original

    def wrapped(*args, **kwargs):
        if len(args) >= 2:
            safe = _normalized_stock_query(args[1])
            args = (args[0], safe, *args[2:])
        elif "query" in kwargs:
            kwargs = dict(kwargs)
            kwargs["query"] = _normalized_stock_query(kwargs["query"])
        else:
            raise RuntimeError("Security V1 could not locate stock-search query")
        return original(*args, **kwargs)

    wrapped._isco_security_v1_search = True
    wrapped._isco_security_v1_original = original
    return wrapped


def _firewall_block_codes(exc: Exception) -> tuple[str, ...]:
    """Return normalized firewall codes only for the exact Security V1 error envelope."""
    message = str(exc).strip()
    if not message.startswith(_FIREWALL_BLOCK_PREFIX):
        return ()
    payload = message[len(_FIREWALL_BLOCK_PREFIX):]
    return tuple(code.strip() for code in payload.split(",") if code.strip())


def _stock_candidate_security_block(exc: Exception) -> dict[str, Any] | None:
    """Quarantine only fully-known candidate-local failures; fail closed otherwise.

    This is failure-scope isolation, not a security bypass. QR/barcode/prompt-like text,
    dense text, unreadable media and candidate-local OCR failures remain rejected before
    cloud Vision. The existing selector may then try another candidate within its normal
    bounded budget. Missing security runtimes, unknown/future codes, and any mixture that
    contains an unknown/hard-fail code are deliberately not converted and still abort.
    """
    codes = _firewall_block_codes(exc)
    if not codes:
        return None
    if any(code in _STOCK_INFRASTRUCTURE_HARD_FAIL_CODES for code in codes):
        return None
    if not set(codes).issubset(_STOCK_CANDIDATE_LOCAL_CODES):
        return None
    rejection = ",".join(codes)
    return {
        "status": "block",
        "relevance": 0.0,
        "visual_quality": 0.0,
        "identifiable_person": False,
        "sensitive_trait_implication_risk": False,
        "prominent_logo_or_brand": False,
        "cultural_conflict": False,
        "cultural_islamic_suitability_risk": False,
        "advertiser_conflict": False,
        "obvious_synthetic_or_visual_artifact": True,
        "reason": (
            "local stock candidate failed fail-closed media/security inspection before "
            "cloud Vision; candidate quarantined"
        ),
        "local_media_rejection": rejection,
    }


def _stock_media_preflight(path: Path) -> dict[str, Any] | None:
    """Inspect every stock candidate before Engine schedules a cloud Vision task.

    Fully-known candidate-local findings remain hard blocks for that asset and are
    returned to the bounded selector/thumbnail board for quarantine. Missing security
    infrastructure and unknown/future findings still abort Production immediately.
    """
    try:
        _scan_media_before_vision(path)
    except RuntimeError as exc:
        blocked = _stock_candidate_security_block(exc)
        if blocked is None:
            raise
        print(
            "Security V1 quarantined stock candidate locally before Vision budget: "
            f"{blocked['local_media_rejection']}"
        )
        return blocked
    return None


def _wrap_vision_audit(
    original: Callable[..., Any], *, isolate_stock_candidate_failures: bool = False
) -> Callable[..., Any]:
    """Legacy generic Vision wrapper retained for strict non-stock call sites/tests.

    Production stock media no longer installs this wrapper: Engine's central preflight
    hook must run before ledger authorization and must inspect every thumbnail-board
    member, neither of which a model-call wrapper can guarantee.
    """
    if getattr(original, "_isco_security_v1_vision", False):
        return original

    def wrapped(*args, **kwargs):
        preview = args[1] if len(args) >= 2 else kwargs.get("preview")
        if preview is None:
            raise RuntimeError("Security V1 could not locate vision preview")
        try:
            _scan_media_before_vision(preview)
        except RuntimeError as exc:
            if isolate_stock_candidate_failures:
                blocked = _stock_candidate_security_block(exc)
                if blocked is not None:
                    print(
                        "Security V1 quarantined stock candidate locally: "
                        f"{blocked['local_media_rejection']}"
                    )
                    return blocked
            raise
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
    # Safe-but-overlong plain-English queries are shortened deterministically at this
    # one boundary before provider access; unsafe content still fails closed.
    orchestrator.pexels_search_videos = _wrap_search(orchestrator.pexels_search_videos)
    orchestrator.pixabay_provider.search_videos = _wrap_search(orchestrator.pixabay_provider.search_videos)
    thumbnail.search_photos = _wrap_search(thumbnail.search_photos)
    thumbnail.pixabay_provider.search_photos = _wrap_search(thumbnail.pixabay_provider.search_photos)

    current_alt = orchestrator.suggest_alternate_visual_query
    if not getattr(current_alt, "_isco_security_v1_alt_query", False):
        def secured_alt_query(*args, **kwargs):
            return _normalized_optional_alternate_query(current_alt(*args, **kwargs))

        secured_alt_query._isco_security_v1_alt_query = True
        secured_alt_query._isco_security_v1_original = current_alt
        orchestrator.suggest_alternate_visual_query = secured_alt_query

    # One central pre-provider boundary now covers long-section video, Opening
    # Director, single-shot/Shorts, and every image in each thumbnail candidate board.
    # Candidate-local blocks are removed before the AI ledger; unknown/infrastructure
    # failures still abort. Engine owns the separate finite inspection/Vision budgets.
    install_stock_media_preflight(_stock_media_preflight)

    _INSTALLED = True
