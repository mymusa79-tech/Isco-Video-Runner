from __future__ import annotations

"""Pinned zero-cost QR confirmation runtime bootstrap for GitHub Actions.

The canonical Production workflow already performs ``apt-get update`` before secrets are
materialized.  This helper deliberately does **not** refresh package metadata and never
contacts an application API.  When the mature QR command-line tools are absent on the
ephemeral Ubuntu runner it installs exact Ubuntu 24.04 (Noble) package versions from the
already-configured OS repositories, using a scrubbed environment and no recommends.

Nothing is written to the repository or persisted as an Actions artifact/cache.  The
installation lives only for the current ephemeral runner.  Outside GitHub Actions the
helper never attempts privilege escalation or package mutation; callers receive a
fail-closed infrastructure error instead.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass


ZBAR_PACKAGE = "zbar-tools"
ZBAR_VERSION = "0.23.93-4build3"
ZXING_PACKAGE = "zxing-cpp-tools"
ZXING_VERSION = "2.2.1-3"
_REQUIRED_TOOLS = ("zbarimg", "ZXingReader")


class QRRuntimeBootstrapError(RuntimeError):
    """Mandatory mature QR runtime is absent, mutable, or failed installation."""


@dataclass(frozen=True, slots=True)
class QRRuntimeTools:
    zbarimg: str
    zxing_reader: str


def _resolved_tools() -> QRRuntimeTools | None:
    zbar = shutil.which("zbarimg")
    zxing = shutil.which("ZXingReader")
    if not zbar or not zxing:
        return None
    return QRRuntimeTools(zbarimg=zbar, zxing_reader=zxing)


def _github_actions() -> bool:
    return str(os.environ.get("GITHUB_ACTIONS") or "").strip().lower() == "true"


def _sanitized_apt_env() -> dict[str, str]:
    # Do not inherit API keys, secret-file paths, Telegram ids, or workflow-specific
    # credentials into the privileged package-manager process.
    return {
        "PATH": os.environ.get("PATH") or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "DEBIAN_FRONTEND": "noninteractive",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _package_version(package: str) -> str:
    dpkg_query = shutil.which("dpkg-query")
    if not dpkg_query:
        raise QRRuntimeBootstrapError("qr_confirmation_package_verifier_unavailable")
    completed = subprocess.run(
        [dpkg_query, "-W", "-f=${Version}", package],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        env=_sanitized_apt_env(),
    )
    if completed.returncode != 0:
        raise QRRuntimeBootstrapError(f"qr_confirmation_package_unverified:{package}")
    return (completed.stdout or "").strip()


def _verify_pinned_versions() -> None:
    expected = {
        ZBAR_PACKAGE: ZBAR_VERSION,
        ZXING_PACKAGE: ZXING_VERSION,
    }
    for package, version in expected.items():
        actual = _package_version(package)
        if actual != version:
            raise QRRuntimeBootstrapError(
                f"qr_confirmation_package_version_mismatch:{package}:{actual}:{version}"
            )


def ensure_qr_confirmation_runtime(*, allow_install: bool = True) -> QRRuntimeTools:
    """Return mature QR tools, installing exact Noble packages only on GitHub Actions.

    Existing tools are accepted only when their owning package versions match the exact
    certified versions.  Missing tools may be installed only on GitHub Actions and only
    when ``allow_install`` is true.  The helper intentionally omits ``apt-get update``:
    canonical workflows already refresh Ubuntu metadata in their dependency step, so a
    later security scan cannot silently widen the supply-chain window.
    """
    tools = _resolved_tools()
    if tools is not None:
        _verify_pinned_versions()
        return tools

    if not allow_install or not _github_actions():
        raise QRRuntimeBootstrapError("qr_confirmation_runtime_unavailable")

    sudo = shutil.which("sudo")
    apt_get = shutil.which("apt-get")
    if not sudo or not apt_get:
        raise QRRuntimeBootstrapError("qr_confirmation_package_manager_unavailable")

    command = [
        sudo,
        "-n",
        apt_get,
        "-o",
        "DPkg::Lock::Timeout=60",
        "install",
        "-y",
        "--no-install-recommends",
        f"{ZBAR_PACKAGE}={ZBAR_VERSION}",
        f"{ZXING_PACKAGE}={ZXING_VERSION}",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=180,
            env=_sanitized_apt_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise QRRuntimeBootstrapError("qr_confirmation_install_timeout") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:240]
        raise QRRuntimeBootstrapError(
            f"qr_confirmation_install_failed:{completed.returncode}:{detail}"
        )

    tools = _resolved_tools()
    if tools is None:
        raise QRRuntimeBootstrapError("qr_confirmation_runtime_unavailable_after_install")
    _verify_pinned_versions()
    return tools
