from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path

from scripts import planning_checkpoint_state as checkpoint

_SNAPSHOT_ENV = "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH"
_PIN_ENV = "ISCO_APPROVED_BRIEF_SHA256"
_ADDITIONAL_CONTRACT_FILES = (
    "scripts/dynamic_planning_capacity.py",
    "scripts/immutable_planning_snapshot.py",
)
_INSTALLED = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_approved_brief(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Copy the approved bytes once and make the snapshot read-only."""
    source = source.resolve()
    destination = destination.resolve()
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise ValueError("approved brief pinned SHA256 is invalid")
    if not source.is_file():
        raise FileNotFoundError(source)

    actual_source = _sha256_file(source)
    if not hmac.compare_digest(actual_source, expected):
        raise ValueError("approved brief source bytes do not match pinned SHA256")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o444)

    actual_snapshot = _sha256_file(destination)
    if not hmac.compare_digest(actual_snapshot, expected):
        raise RuntimeError("immutable approved brief snapshot SHA256 mismatch")
    if destination.stat().st_mode & 0o222:
        raise RuntimeError("immutable approved brief snapshot is still writable")
    return destination


def _snapshot_path() -> Path:
    raw = str(os.environ.get(_SNAPSHOT_ENV) or "").strip()
    if not raw:
        raise RuntimeError(f"{_SNAPSHOT_ENV} is required for canonical durable planning")
    path = Path(raw).resolve()
    if not path.is_file():
        raise RuntimeError("immutable approved brief snapshot is missing")
    if path.stat().st_mode & 0o222:
        raise RuntimeError("immutable approved brief snapshot must be read-only")
    return path


def _install_snapshot_binding() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    for relative in _ADDITIONAL_CONTRACT_FILES:
        if relative not in checkpoint.PLANNING_CONTRACT_FILES:
            checkpoint.PLANNING_CONTRACT_FILES = tuple(checkpoint.PLANNING_CONTRACT_FILES) + (relative,)

    def build_snapshot_binding(repo_root: Path, engine_root: Path) -> checkpoint.Binding:
        brief = _snapshot_path()
        expected_brief = str(os.environ.get(_PIN_ENV) or "").strip().lower()
        actual_brief = _sha256_file(brief)
        if not expected_brief:
            expected_brief = actual_brief
        engine_head = checkpoint._git_value(
            checkpoint._run,
            ["git", "rev-parse", "HEAD"],
            cwd=engine_root.resolve(),
        )
        if not engine_head:
            raise ValueError("could not resolve Engine checkout")
        return checkpoint.build_binding(
            brief_path=brief,
            expected_brief_sha256=expected_brief,
            engine_repo=engine_root,
            expected_engine_sha=str(os.environ.get("ISCO_ENGINE_SHA") or engine_head),
            contract_root=repo_root,
        )

    checkpoint.build_runtime_binding = build_snapshot_binding
    _INSTALLED = True


def bootstrap_immutable_planning_checkpoint(
    *,
    repo_root: Path,
    engine_root: Path,
    encryption_key: str,
) -> checkpoint.RestoreStatus:
    """Bind restore/persist to the immutable snapshot before install_router() reads cache."""
    if not checkpoint.canonical_runtime_enabled():
        return checkpoint.RestoreStatus(True, False, "disabled", "non-canonical runtime")
    if not str(encryption_key or "").strip():
        raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint bootstrap")

    _install_snapshot_binding()
    status = checkpoint.bootstrap_runtime_restore(
        repo_root=repo_root,
        engine_root=engine_root,
        key=str(encryption_key).strip(),
    )
    # Secret is already owner-only on disk after bootstrap; do not leave the environment
    # copy available to the rest of production.
    os.environ.pop("STATE_ENCRYPTION_KEY", None)
    print(
        "Immutable approved-brief checkpoint binding PASS: "
        f"snapshot={_snapshot_path().name} sha256={_sha256_file(_snapshot_path())[:12]}..."
    )
    return status
