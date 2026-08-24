from __future__ import annotations

import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import requests


EXPECTED_OS_VERSION = "24.04"
EXPECTED_PYTHON = (3, 12)
EXPECTED_PIPER = "1.4.2"


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


def _release_namespace_status(repository: str, release_tag: str, *, timeout: int = 15) -> str:
    url = f"https://api.github.com/repos/{repository}/releases/tags/{release_tag}"
    response = requests.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    if response.status_code == 404:
        return "available"
    if response.ok:
        raise RuntimeError(f"existing release tag blocks this run before production: {release_tag}")
    if response.status_code == 403:
        # Never guess that a 403 means absence; rate limiting or policy could hide a collision.
        raise RuntimeError("release namespace preflight could not prove tag availability: HTTP 403")
    if response.status_code == 429 or 500 <= response.status_code <= 599:
        raise RuntimeError(f"release namespace preflight unavailable: HTTP {response.status_code}")
    raise RuntimeError(f"release namespace preflight failed: HTTP {response.status_code}")


def run_environment_preflight(*, output: Path, repository: str, run_number: str) -> EnvironmentEvidence:
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

    # pip check happens after Piper has mutated the environment, not before it.
    check = subprocess.run([sys.executable, "-m", "pip", "check"], text=True, capture_output=True)
    if check.returncode != 0:
        raise RuntimeError("post-Piper dependency graph is inconsistent")

    ffmpeg = _binary("ffmpeg")
    ffprobe = _binary("ffprobe")
    tesseract = _binary("tesseract")
    release_tag = f"video-{run_number}"
    release_namespace = _release_namespace_status(repository, release_tag)

    evidence = EnvironmentEvidence(
        os_version=os_version,
        python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        piper_tts=piper,
        onnxruntime=onnxruntime,
        pathvalidate=pathvalidate,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        tesseract=tesseract,
        release_tag=release_tag,
        release_namespace=release_namespace,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 1, **asdict(evidence)}, indent=2), encoding="utf-8")
    tmp.replace(output)
    return evidence


def main() -> None:
    repository = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    run_number = (os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    if not repository or not run_number:
        raise RuntimeError("GitHub run identity is required for production environment preflight")
    output = Path(os.environ.get("RUNNER_TEMP") or ".") / "preproduction-environment.json"
    evidence = run_environment_preflight(output=output, repository=repository, run_number=run_number)
    print(
        "Environment preflight PASS: "
        f"Ubuntu {evidence.os_version}, Python {evidence.python}, Piper {evidence.piper_tts}, "
        f"release namespace {evidence.release_namespace}"
    )


if __name__ == "__main__":
    main()
