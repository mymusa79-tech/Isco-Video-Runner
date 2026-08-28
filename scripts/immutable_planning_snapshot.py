from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

try:
    from scripts import planning_checkpoint_state as checkpoint
except ModuleNotFoundError:  # direct python scripts/persistent_memory.py compatibility
    import planning_checkpoint_state as checkpoint

_SNAPSHOT_ENV = "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH"
_PIN_ENV = "ISCO_APPROVED_BRIEF_SHA256"
_RUNTIME_BRIEF_ENV = "ISCO_APPROVED_BRIEF_PATH"
_SNAPSHOT_FILENAME = "approved-brief.snapshot.json"
_COMMITTED_BRIEF_PATH = "production/approved_brief.json"
_INSTALLED = False


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_expected_sha256(value: str) -> str:
    expected = str(value or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise ValueError("approved brief pinned SHA256 is invalid")
    return expected


def snapshot_approved_brief(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Copy approved bytes from an explicit source and make the snapshot read-only."""
    source = source.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    return snapshot_approved_brief_bytes(
        source.read_bytes(),
        destination,
        expected_sha256=expected_sha256,
    )


def snapshot_approved_brief_bytes(
    payload: bytes,
    destination: Path,
    *,
    expected_sha256: str,
) -> Path:
    """Materialize one verified immutable snapshot from already-trusted bytes."""
    destination = destination.resolve()
    expected = _validate_expected_sha256(expected_sha256)
    actual_source = _sha256_bytes(payload)
    if not hmac.compare_digest(actual_source, expected):
        raise ValueError("approved brief source bytes do not match pinned SHA256")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.chmod(0o444)

    actual_snapshot = _sha256_file(destination)
    if not hmac.compare_digest(actual_snapshot, expected):
        raise RuntimeError("immutable approved brief snapshot SHA256 mismatch")
    if destination.stat().st_mode & 0o222:
        raise RuntimeError("immutable approved brief snapshot is still writable")
    return destination


def _committed_approved_brief_bytes(engine_root: Path) -> bytes:
    """Read the approved brief from the pinned Engine commit, never its mutable worktree."""
    engine = engine_root.resolve()
    shown = checkpoint._run(
        ["git", "show", f"HEAD:{_COMMITTED_BRIEF_PATH}"],
        cwd=engine,
    )
    if shown.returncode != 0 or not shown.stdout:
        raise RuntimeError("could not read approved brief from pinned Engine commit")
    return bytes(shown.stdout)


def _persist_snapshot_env(path: Path) -> None:
    os.environ[_SNAPSHOT_ENV] = str(path)
    github_env = str(os.environ.get("GITHUB_ENV") or "").strip()
    if not github_env:
        return
    with Path(github_env).open("a", encoding="utf-8") as handle:
        handle.write(f"{_SNAPSHOT_ENV}={path}\n")


def _worktree_brief_drift(engine_root: Path, expected_sha256: str) -> bool:
    path = engine_root.resolve() / _COMMITTED_BRIEF_PATH
    if not path.is_file():
        return True
    return not hmac.compare_digest(_sha256_file(path), expected_sha256)


def materialize_runtime_snapshot(repo_root: Path, engine_root: Path) -> Path:
    """Create the run snapshot from the pinned Engine commit, not the mutable worktree.

    GitHub Actions steps share one workspace. Test suites may therefore accidentally
    mutate tracked files before production. The approved production input must remain
    attached to source identity, so canonical V4 reads the committed blob at Engine
    HEAD and only uses the worktree for drift diagnostics.
    """
    del repo_root
    if not checkpoint.canonical_runtime_enabled():
        raise RuntimeError("immutable planning snapshot is canonical-runtime only")

    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise RuntimeError("RUNNER_TEMP is required for immutable approved brief snapshot")
    destination = Path(temp).resolve() / "isco-state" / _SNAPSHOT_FILENAME

    committed = _committed_approved_brief_bytes(engine_root)
    committed_sha = _sha256_bytes(committed)
    expected = str(os.environ.get(_PIN_ENV) or "").strip().lower() or committed_sha
    expected = _validate_expected_sha256(expected)
    if not hmac.compare_digest(committed_sha, expected):
        raise ValueError("pinned Engine commit approved brief does not match pinned SHA256")

    if destination.is_file():
        # One-copy rule: never refresh a snapshot after the run has started.
        if destination.stat().st_mode & 0o222:
            raise RuntimeError("existing approved brief snapshot is writable")
        actual = _sha256_file(destination)
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError("existing approved brief snapshot no longer matches pinned SHA256")
        _persist_snapshot_env(destination)
        return destination

    snapshot = snapshot_approved_brief_bytes(
        committed,
        destination,
        expected_sha256=expected,
    )
    _persist_snapshot_env(snapshot)

    if _worktree_brief_drift(engine_root, expected):
        print(
            "APPROVED_BRIEF_WORKTREE_DRIFT detected=true "
            "production_source=git_head_snapshot action=ignore_mutable_worktree"
        )
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


def bind_runtime_approved_brief_path() -> Path:
    """Make every in-process Engine reader consume the immutable approved snapshot."""
    path = _snapshot_path()
    expected = str(os.environ.get(_PIN_ENV) or "").strip().lower()
    if expected:
        expected = _validate_expected_sha256(expected)
        if not hmac.compare_digest(_sha256_file(path), expected):
            raise RuntimeError("immutable approved brief snapshot does not match pinned SHA256")
    os.environ[_RUNTIME_BRIEF_ENV] = str(path)
    return path


def install_runtime_snapshot_binding(*, force: bool = False) -> None:
    """Use the immutable run snapshot as both production input and checkpoint identity.

    Unit/regression processes are intentionally left untouched unless force=True, so a
    test that imports runtime_closure cannot leak snapshot requirements into unrelated
    checkpoint tests running later in the same Python process.

    The checkpoint contract itself is not extended manually here. Current Runner main
    derives a deterministic transitive source closure from run_v3_voice.py, so these
    snapshot/capacity modules enter the binding automatically when reachable from the
    canonical production entrypoint. That is stricter and avoids a second mutable list.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    if not force and not checkpoint.canonical_runtime_enabled():
        return

    bind_runtime_approved_brief_path()

    def build_snapshot_binding(repo_root: Path, engine_root: Path) -> checkpoint.Binding:
        brief = bind_runtime_approved_brief_path()
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
