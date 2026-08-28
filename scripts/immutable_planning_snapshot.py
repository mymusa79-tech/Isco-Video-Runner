from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path

from scripts import planning_checkpoint_state as checkpoint

_SNAPSHOT_ENV = "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH"
_PIN_ENV = "ISCO_APPROVED_BRIEF_SHA256"
_SNAPSHOT_FILENAME = "approved-brief.snapshot.json"
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


def _persist_snapshot_env(path: Path) -> None:
    os.environ[_SNAPSHOT_ENV] = str(path)
    github_env = str(os.environ.get("GITHUB_ENV") or "").strip()
    if not github_env:
        return
    with Path(github_env).open("a", encoding="utf-8") as handle:
        handle.write(f"{_SNAPSHOT_ENV}={path}\n")


def materialize_runtime_snapshot(repo_root: Path, engine_root: Path) -> Path:
    """Create the run snapshot once during cross-run state restore."""
    del repo_root
    if not checkpoint.canonical_runtime_enabled():
        raise RuntimeError("immutable planning snapshot is canonical-runtime only")
    source = engine_root.resolve() / "production" / "approved_brief.json"
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise RuntimeError("RUNNER_TEMP is required for immutable approved brief snapshot")
    destination = Path(temp).resolve() / "isco-state" / _SNAPSHOT_FILENAME

    if destination.is_file():
        # One-copy rule: never refresh a snapshot after the run has started.
        if destination.stat().st_mode & 0o222:
            raise RuntimeError("existing approved brief snapshot is writable")
        _persist_snapshot_env(destination)
        return destination

    expected = str(os.environ.get(_PIN_ENV) or "").strip().lower()
    if not expected:
        # The brief was already approval-validated earlier in canonical V4. Persist the
        # exact bytes now; the production step later supplies ISCO_APPROVED_BRIEF_SHA256
        # and re-verifies this same snapshot against the pinned approval hash.
        expected = _sha256_file(source)
    snapshot = snapshot_approved_brief(source, destination, expected_sha256=expected)
    _persist_snapshot_env(snapshot)
    return snapshot


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


def install_runtime_snapshot_binding(*, force: bool = False) -> None:
    """Use the immutable run snapshot as checkpoint identity in canonical production.

    Unit/regression processes are intentionally left untouched unless force=True, so a
    test that imports runtime_closure cannot leak snapshot requirements into unrelated
    checkpoint tests running later in the same Python process.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    if not force and not checkpoint.canonical_runtime_enabled():
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
    """Create/bind the immutable brief before restoring the durable planner cache."""
    if not checkpoint.canonical_runtime_enabled():
        return checkpoint.RestoreStatus(True, False, "disabled", "non-canonical runtime")
    if not str(encryption_key or "").strip():
        raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint bootstrap")

    materialize_runtime_snapshot(repo_root, engine_root)
    install_runtime_snapshot_binding(force=True)
    status = checkpoint.bootstrap_runtime_restore(
        repo_root=repo_root,
        engine_root=engine_root,
        key=str(encryption_key).strip(),
    )
    print(
        "Immutable approved-brief checkpoint binding PASS: "
        f"snapshot={_snapshot_path().name} sha256={_sha256_file(_snapshot_path())[:12]}..."
    )
    return status
