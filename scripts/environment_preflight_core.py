from __future__ import annotations

import importlib.metadata as md
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

import requests


EXPECTED_OS_VERSION = "24.04"
EXPECTED_PYTHON = (3, 12)
EXPECTED_PIPER = "1.4.2"
MIN_FREE_BYTES = 6 * 1024 * 1024 * 1024
REQUIRED_FFMPEG_FILTERS = {"blackdetect", "silencedetect", "freezedetect", "loudnorm", "subtitles"}
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class EnvironmentEvidence:
    os_version: str
    python: str
    piper_tts: str
    onnxruntime: str
    pathvalidate: str
    ffmpeg: str
    ffprobe: str
    tesseract: str
    openssl: str
    gh: str
    arabic_font: str
    free_bytes: int
    ffmpeg_libx264: bool
    ffmpeg_filters: list[str]
    tesseract_arabic: bool
    release_tag: str
    release_namespace: str


def _version(name: str) -> str:
    try:
        return md.version(name)
    except md.PackageNotFoundError as exc:
        raise RuntimeError(f"required runtime package missing: {name}") from exc


def _binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"required runtime binary missing: {name}")
    return path


def _secret_free_env() -> dict[str, str]:
    markers = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    return {key: value for key, value in os.environ.items() if not any(marker in key.upper() for marker in markers)}


def _run_local(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
        env=_secret_free_env(),
    )


def _require_command(args: list[str], *, description: str, timeout: int = 30) -> str:
    result = _run_local(args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"required runtime capability failed: {description}")
    return result.stdout + result.stderr


def _certify_ffmpeg() -> tuple[bool, list[str]]:
    encoders = _require_command(["ffmpeg", "-hide_banner", "-encoders"], description="ffmpeg encoders")
    if "libx264" not in encoders:
        raise RuntimeError("required ffmpeg encoder missing: libx264")
    filters_raw = _require_command(["ffmpeg", "-hide_banner", "-filters"], description="ffmpeg filters")
    missing = sorted(name for name in REQUIRED_FFMPEG_FILTERS if name not in filters_raw)
    if missing:
        raise RuntimeError("required ffmpeg filters missing: " + ", ".join(missing))
    return True, sorted(REQUIRED_FFMPEG_FILTERS)


def _certify_tesseract_arabic() -> bool:
    languages = _require_command(["tesseract", "--list-langs"], description="Tesseract language list")
    available = {line.strip() for line in languages.splitlines() if line.strip()}
    if "ara" not in available:
        raise RuntimeError("required Tesseract Arabic language data missing: ara")
    return True


def _certify_arabic_font() -> str:
    result = _run_local(["fc-match", "Noto Sans Arabic", "--format=%{family}"])
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("required Arabic font resolution failed")
    return result.stdout.strip()[:160]


def _same_target_release_state(release: requests.Response, *, release_tag: str, target_sha: str) -> str:
    target_sha = target_sha.strip().lower()
    if not SHA1_RE.fullmatch(target_sha):
        raise RuntimeError("existing release cannot be reconciled without exact current Runner SHA")
    try:
        payload = release.json()
    except Exception as exc:
        raise RuntimeError("existing release reconciliation returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("existing release reconciliation returned non-object JSON")
    if str(payload.get("tag_name") or "") != release_tag:
        raise RuntimeError("existing release tag identity does not match requested namespace")
    remote_target = str(payload.get("target_commitish") or "").strip().lower()
    if remote_target != target_sha:
        raise RuntimeError("existing release tag belongs to a different Runner SHA")
    draft = payload.get("draft")
    if draft is True:
        return "reconcile_existing_draft"
    if draft is False:
        return "reconcile_existing_published"
    raise RuntimeError("existing release draft state is malformed")


def _release_namespace_status(
    repository: str,
    release_tag: str,
    *,
    target_sha: str = "",
    token: str = "",
    timeout: int = 15,
) -> str:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token

    encoded_tag = quote(release_tag, safe="")
    release_url = f"https://api.github.com/repos/{repository}/releases/tags/{encoded_tag}"
    release = requests.get(release_url, headers=headers, timeout=timeout)
    if release.ok:
        if not target_sha:
            raise RuntimeError(f"existing release tag blocks this run before production: {release_tag}")
        return _same_target_release_state(
            release,
            release_tag=release_tag,
            target_sha=target_sha,
        )
    if release.status_code != 404:
        if release.status_code == 403:
            raise RuntimeError("release namespace preflight could not prove release availability: HTTP 403")
        if release.status_code == 429 or 500 <= release.status_code <= 599:
            raise RuntimeError(f"release namespace preflight unavailable: HTTP {release.status_code}")
        raise RuntimeError(f"release namespace preflight failed: HTTP {release.status_code}")

    ref_url = f"https://api.github.com/repos/{repository}/git/ref/tags/{encoded_tag}"
    ref = requests.get(ref_url, headers=headers, timeout=timeout)
    if ref.status_code == 404:
        return "available"
    if ref.ok:
        raise RuntimeError(f"existing orphan Git tag blocks this run before production: {release_tag}")
    if ref.status_code == 403:
        raise RuntimeError("release namespace preflight could not prove Git tag availability: HTTP 403")
    if ref.status_code == 429 or 500 <= ref.status_code <= 599:
        raise RuntimeError(f"release namespace preflight unavailable: HTTP {ref.status_code}")
    raise RuntimeError(f"release Git tag namespace preflight failed: HTTP {ref.status_code}")


def run_environment_preflight(
    *,
    output: Path,
    repository: str,
    run_number: str,
    github_token: str = "",
    target_sha: str = "",
    release_tag: str = "",
) -> EnvironmentEvidence:
    os_release = platform.freedesktop_os_release()
    os_version = str(os_release.get("VERSION_ID") or "")
    if os_version != EXPECTED_OS_VERSION:
        raise RuntimeError(f"runner OS drift detected: expected {EXPECTED_OS_VERSION}, got {os_version or 'unknown'}")
    if sys.version_info[:2] != EXPECTED_PYTHON:
        raise RuntimeError(f"Python drift detected: expected 3.12, got {sys.version_info.major}.{sys.version_info.minor}")

    piper = _version("piper-tts")
    if piper != EXPECTED_PIPER:
        raise RuntimeError(f"Piper drift detected: expected {EXPECTED_PIPER}, got {piper}")
    onnxruntime = _version("onnxruntime")
    pathvalidate = _version("pathvalidate")

    check = _run_local([sys.executable, "-m", "pip", "check"], timeout=60)
    if check.returncode != 0:
        raise RuntimeError("post-Piper dependency graph is inconsistent")

    ffmpeg = _binary("ffmpeg")
    ffprobe = _binary("ffprobe")
    tesseract = _binary("tesseract")
    openssl = _binary("openssl")
    gh = _binary("gh")
    _binary("fc-match")

    free_bytes = shutil.disk_usage(Path.cwd()).free
    if free_bytes < MIN_FREE_BYTES:
        raise RuntimeError(f"insufficient free disk before production: {free_bytes} bytes")

    ffmpeg_libx264, ffmpeg_filters = _certify_ffmpeg()
    tesseract_arabic = _certify_tesseract_arabic()
    arabic_font = _certify_arabic_font()
    _require_command(["ffprobe", "-version"], description="ffprobe version")
    _require_command(["openssl", "version"], description="OpenSSL")
    _require_command(["gh", "--version"], description="GitHub CLI")

    release_tag = str(release_tag or "").strip() or f"video-{run_number}"
    release_namespace = _release_namespace_status(
        repository,
        release_tag,
        target_sha=target_sha,
        token=github_token,
    )

    evidence = EnvironmentEvidence(
        os_version=os_version,
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        piper_tts=piper,
        onnxruntime=onnxruntime,
        pathvalidate=pathvalidate,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        tesseract=tesseract,
        openssl=openssl,
        gh=gh,
        arabic_font=arabic_font,
        free_bytes=free_bytes,
        ffmpeg_libx264=ffmpeg_libx264,
        ffmpeg_filters=ffmpeg_filters,
        tesseract_arabic=tesseract_arabic,
        release_tag=release_tag,
        release_namespace=release_namespace,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 2, **asdict(evidence)}, indent=2), encoding="utf-8")
    tmp.replace(output)
    return evidence


def main() -> None:
    repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    run_number = (os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    target_sha = (os.environ.get("GITHUB_SHA") or "").strip().lower()
    if not repository or not run_number or not SHA1_RE.fullmatch(target_sha):
        raise RuntimeError("exact GitHub run identity is required for production environment preflight")
    output = Path(os.environ.get("RUNNER_TEMP") or ".") / "preproduction-environment.json"
    evidence = run_environment_preflight(
        output=output,
        repository=repository,
        run_number=run_number,
        github_token=(os.environ.get("GITHUB_TOKEN") or "").strip(),
        target_sha=target_sha,
        release_tag=(os.environ.get("ISCO_RELEASE_TAG_OVERRIDE") or "").strip(),
    )
    print(
        "Environment preflight PASS: "
        f"Ubuntu {evidence.os_version}, Python {evidence.python}, Piper {evidence.piper_tts}, "
        f"free={evidence.free_bytes}, release namespace {evidence.release_namespace}"
    )


if __name__ == "__main__":
    main()
