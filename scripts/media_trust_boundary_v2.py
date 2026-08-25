from __future__ import annotations

import atexit
import hashlib
import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.stock_media_preflight as stock_preflight
import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.providers import pexels as pexels_provider
from isco_video_agent.providers import pixabay as pixabay_provider
import scripts.security_v1_live_binding as security_v1


MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
MAX_REDIRECTS = 4
VIDEO_MIN_SAMPLES = 3
VIDEO_MAX_SAMPLES = 8
VIDEO_SAMPLE_SPACING_SECONDS = 15.0
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".m4v"})
_ALLOWED_CONTENT_TYPES = (
    "video/",
    "image/",
    "application/octet-stream",
    "binary/octet-stream",
)
_INSTALLED = False
_http_get: Callable[..., Any] = requests.get
_quarantine_root: Path | None = None
_original_make_review_preview = orchestrator.make_review_preview
_original_make_image_review_preview = thumbnail.make_image_review_preview


@dataclass(frozen=True)
class TrustedMediaRecord:
    provider: str
    source_url: str
    final_url: str
    sha256: str
    byte_length: int
    quarantine_path: Path


_records_by_url: dict[tuple[str, str], TrustedMediaRecord] = {}
_records_by_path: dict[str, TrustedMediaRecord] = {}
_review_source_by_preview: dict[str, Path] = {}


def _path_key(path: str | Path) -> str:
    return str(Path(path).resolve())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _root() -> Path:
    global _quarantine_root
    if _quarantine_root is None:
        _quarantine_root = Path(tempfile.mkdtemp(prefix="isco-media-trust-v2-"))
    return _quarantine_root


def _cleanup_quarantine() -> None:
    global _quarantine_root
    if _quarantine_root is not None:
        shutil.rmtree(_quarantine_root, ignore_errors=True)
        _quarantine_root = None


atexit.register(_cleanup_quarantine)


def reset_media_trust_state_for_tests() -> None:
    """Clear process-local trust state; tests only, never called by Production."""
    global _INSTALLED
    _records_by_url.clear()
    _records_by_path.clear()
    _review_source_by_preview.clear()
    _cleanup_quarantine()
    _INSTALLED = False


def _provider_host_allowed(provider: str, host: str) -> bool:
    host = host.casefold().strip(".")
    if provider == "pexels":
        return host == "pexels.com" or host.endswith(".pexels.com")
    if provider == "pixabay":
        return host == "pixabay.com" or host.endswith(".pixabay.com")
    return False


def _validate_provider_url(provider: str, url: str) -> None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not _provider_host_allowed(provider, host):
        raise RuntimeError(f"media_trust_unapproved_{provider}_url")
    if parsed.username or parsed.password:
        raise RuntimeError("media_trust_url_credentials_rejected")


def _validate_content_type(response: Any) -> None:
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().casefold()
    if not content_type:
        return
    if not any(content_type.startswith(prefix) for prefix in _ALLOWED_CONTENT_TYPES):
        raise RuntimeError(f"media_trust_unexpected_content_type:{content_type[:80]}")


def _stream_to_quarantine(provider: str, source_url: str, cache_path: Path) -> TrustedMediaRecord:
    current_url = source_url
    part = cache_path.with_suffix(cache_path.suffix + ".part")
    part.unlink(missing_ok=True)
    digest = hashlib.sha256()
    total = 0
    final_url = source_url

    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            _validate_provider_url(provider, current_url)
            response = _http_get(
                current_url,
                stream=True,
                timeout=120,
                allow_redirects=False,
            )
            try:
                status_code = int(getattr(response, "status_code", 0))
                if status_code in _REDIRECT_STATUSES:
                    if redirect_count >= MAX_REDIRECTS:
                        raise RuntimeError("media_trust_redirect_limit_exceeded")
                    location = str(response.headers.get("location") or "").strip()
                    if not location:
                        raise RuntimeError("media_trust_redirect_without_location")
                    next_url = urljoin(current_url, location)
                    _validate_provider_url(provider, next_url)
                    current_url = next_url
                    continue

                response.raise_for_status()
                _validate_content_type(response)
                declared_raw = str(response.headers.get("content-length") or "").strip()
                if declared_raw:
                    try:
                        declared = int(declared_raw)
                    except ValueError as exc:
                        raise RuntimeError("media_trust_invalid_content_length") from exc
                    if declared < 0 or declared > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("media_trust_download_size_limit")

                with part.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("media_trust_download_size_limit")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                final_url = current_url
                break
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        else:
            raise RuntimeError("media_trust_redirect_limit_exceeded")

        if total <= 0 or not part.is_file():
            raise RuntimeError("media_trust_empty_download")
        os.replace(part, cache_path)
        return TrustedMediaRecord(
            provider=provider,
            source_url=source_url,
            final_url=final_url,
            sha256=digest.hexdigest(),
            byte_length=total,
            quarantine_path=cache_path,
        )
    except Exception:
        part.unlink(missing_ok=True)
        raise


def _materialize_verified(record: TrustedMediaRecord, dest: Path) -> Path:
    if not record.quarantine_path.is_file():
        raise RuntimeError("media_trust_quarantine_missing")
    if _sha256_file(record.quarantine_path) != record.sha256:
        raise RuntimeError("media_trust_quarantine_hash_mismatch")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".media-trust-v2.tmp")
    tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(record.quarantine_path, tmp)
        with tmp.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if _sha256_file(tmp) != record.sha256:
            raise RuntimeError("media_trust_materialization_hash_mismatch")
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    _records_by_path[_path_key(dest)] = record
    return dest


def trusted_download(provider: str, url: str, dest: Path) -> Path:
    provider = str(provider).strip().casefold()
    source_url = str(url).strip()
    _validate_provider_url(provider, source_url)
    key = (provider, source_url)
    record = _records_by_url.get(key)
    if record is None:
        suffix = Path(urlparse(source_url).path).suffix.lower()
        if len(suffix) > 10:
            suffix = ""
        cache_name = hashlib.sha256((provider + "\x1f" + source_url).encode("utf-8")).hexdigest() + (suffix or ".bin")
        record = _stream_to_quarantine(provider, source_url, _root() / cache_name)
        _records_by_url[key] = record
    return _materialize_verified(record, Path(dest))


def trusted_record(path: str | Path) -> TrustedMediaRecord | None:
    try:
        return _records_by_path.get(_path_key(path))
    except OSError:
        return None


def _review_exact_render_variant(video: dict, portrait: bool = False) -> dict | None:
    """Review the exact provider variant the renderer would otherwise choose."""
    return pexels_provider.best_file(video, portrait=portrait)


def _sample_timestamps(duration_seconds: float) -> list[float]:
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(duration) or duration <= 0:
        return []
    if duration <= 0.30:
        return [0.0]
    target = int(math.ceil(duration / VIDEO_SAMPLE_SPACING_SECONDS)) + 1
    count = max(VIDEO_MIN_SAMPLES, min(VIDEO_MAX_SAMPLES, target))
    end = max(0.0, duration - 0.10)
    if count == 1:
        return [0.0]
    return [round(end * index / (count - 1), 3) for index in range(count)]


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
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_timeout") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_failed")
    try:
        duration = float(completed.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_failed") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_failed")
    return duration


def _distributed_scan_media_before_vision(media: str | Path) -> None:
    """Fail closed using bounded samples distributed across the complete trusted asset."""
    source = Path(media)
    if not source.is_file():
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}media_missing")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}ffmpeg_unavailable")
    if not shutil.which("tesseract"):
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}ocr_runtime_unavailable")

    record = trusted_record(source)
    if record is not None and _sha256_file(source) != record.sha256:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_unreadable")

    video = source.suffix.lower() in _VIDEO_SUFFIXES
    ffprobe = shutil.which("ffprobe") if video else None
    if video and not ffprobe:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}ffmpeg_unavailable")
    timestamps = _sample_timestamps(_probe_duration(source, ffprobe)) if video else [0.0]
    if not timestamps:
        raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_failed")

    with tempfile.TemporaryDirectory(prefix="isco-security-frame-v2-") as tmp:
        root = Path(tmp)
        firewall = security_v1.MultimodalInjectionFirewall(ocr_backend=security_v1._production_ocr)
        for index, timestamp in enumerate(timestamps, 1):
            frame = root / f"frame-{index:02d}.pgm"
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            if video:
                command.extend(["-ss", f"{timestamp:.3f}"])
            command.extend(
                [
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=640:-2:force_original_aspect_ratio=decrease,format=gray",
                    str(frame),
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
                raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_timeout") from exc
            if completed.returncode != 0 or not frame.is_file() or frame.stat().st_size <= 0:
                raise RuntimeError(f"{security_v1._FIREWALL_BLOCK_PREFIX}frame_extract_failed")
            security_v1.require_normal_vision_safe(firewall.scan_frame(frame))


def _register_preview(preview: Path, source: Path) -> Path:
    _review_source_by_preview[_path_key(preview)] = Path(source)
    return preview


def _make_trusted_review_preview(src: Path, dest: Path, *, portrait: bool, seconds: float = 6.0) -> Path:
    preview = _original_make_review_preview(src, dest, portrait=portrait, seconds=seconds)
    return _register_preview(preview, src)


def _make_trusted_image_review_preview(src: Path, dest: Path) -> Path:
    preview = _original_make_image_review_preview(src, dest)
    return _register_preview(preview, src)


def _inspect_exact_review_source(path: str | Path) -> dict | None:
    """Inspect the quarantined source, never the shortened/resized review derivative."""
    key = _path_key(path)
    source = _review_source_by_preview.get(key, Path(path))
    return stock_preflight.inspect_stock_media(source)


def _pexels_download(url: str, dest: Path) -> Path:
    return trusted_download("pexels", url, dest)


def _pixabay_download(url: str, dest: Path) -> Path:
    return trusted_download("pixabay", url, dest)


def install_media_trust_boundary_v2() -> None:
    """Bind exact review/render bytes without changing selection or Vision budgets."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Exact-variant invariant: semantic review and final render use the same provider file.
    pexels_provider.review_file = _review_exact_render_variant
    pixabay_provider.review_file = _review_exact_render_variant
    orchestrator.review_file = _review_exact_render_variant

    # One network acquisition per URL. Every later consumer receives an atomically
    # materialized copy whose SHA256 must match the quarantined bytes.
    pexels_provider.download = _pexels_download
    pixabay_provider.download = _pixabay_download
    orchestrator.pexels_download = _pexels_download
    orchestrator.pixabay_provider.download = _pixabay_download
    if hasattr(thumbnail, "download"):
        thumbnail.download = _pexels_download
    thumbnail.pixabay_provider.download = _pixabay_download

    # Previews remain compact for cloud Vision, but Security V1 is redirected back to
    # the exact trusted source bytes from which each preview was derived.
    orchestrator.make_review_preview = _make_trusted_review_preview
    orchestrator.inspect_stock_media = _inspect_exact_review_source
    thumbnail.make_image_review_preview = _make_trusted_image_review_preview
    thumbnail.inspect_stock_media = _inspect_exact_review_source

    # Replace only local media sampling. Security decisions, candidate isolation,
    # Vision counts and fail-closed semantics remain owned by Security V1/Engine.
    security_v1._scan_media_before_vision = _distributed_scan_media_before_vision

    _INSTALLED = True
    print(
        "Media Trust Boundary V2 installed: exact review/render bytes, SHA256 quarantine, "
        "manual redirects, atomic materialization, distributed source scan"
    )
