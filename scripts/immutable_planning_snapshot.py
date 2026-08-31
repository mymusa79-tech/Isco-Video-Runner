from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

try:
    from scripts import planning_checkpoint_state as checkpoint
    from scripts.runtime_phase import canonical_runtime_enabled
except ModuleNotFoundError:  # direct python scripts/persistent_memory.py compatibility
    import planning_checkpoint_state as checkpoint
    from runtime_phase import canonical_runtime_enabled

_SNAPSHOT_ENV = "ISCO_APPROVED_BRIEF_SNAPSHOT_PATH"
_SNAPSHOT_SHA_ENV = "ISCO_APPROVED_BRIEF_SNAPSHOT_SHA256"
_RUNTIME_BRIEF_ENV = "ISCO_APPROVED_BRIEF_PATH"
_SNAPSHOT_FILENAME = "approved-brief.snapshot.json"
_COMMITTED_BRIEF_PATH = "production/approved_brief.json"
_CONTROL_REQUEST_PATH_ENV = "ISCO_CONTROL_REQUEST_PATH"
_CONTROL_REQUEST_SHA_ENV = "ISCO_CONTROL_REQUEST_SHA256"
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


def _telegram_runtime_approved_brief_bytes(engine_root: Path) -> bytes | None:
    """Return the exact Telegram-approved brief before the compatibility worktree is reset.

    PR #455 originally materialized the Telegram brief into the tracked Engine fixture so
    the existing validation step could consume it. Canonical runtime must not keep that
    mutation as production source. When Telegram ingress is active, validate the dynamic
    brief against the exact durable request identity, then snapshot those bytes outside
    the Engine checkout. Any partial/mismatched ingress fails closed.
    """
    request_value = str(os.environ.get(_CONTROL_REQUEST_PATH_ENV) or "").strip()
    request_sha = str(os.environ.get(_CONTROL_REQUEST_SHA_ENV) or "").strip().lower()
    if not request_value and not request_sha:
        return None
    if not request_value or not request_sha:
        raise RuntimeError("Telegram approved-brief snapshot requires request path and SHA together")
    _validate_expected_sha256(request_sha)

    request_path = Path(request_value).resolve()
    if not request_path.is_file():
        raise RuntimeError("Telegram approved request copy is missing before immutable snapshot")
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Telegram approved request copy is invalid JSON") from exc
    if not isinstance(request, dict):
        raise RuntimeError("Telegram approved request copy must be an object")
    if str(request.get("request_sha256") or "").strip().lower() != request_sha:
        raise RuntimeError("Telegram approved request copy no longer matches dispatch SHA")

    brief_path = engine_root.resolve() / _COMMITTED_BRIEF_PATH
    if not brief_path.is_file():
        raise RuntimeError("Telegram materialized approved brief is missing from compatibility path")
    payload = brief_path.read_bytes()
    try:
        brief = json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Telegram materialized approved brief is invalid JSON") from exc
    if not isinstance(brief, dict) or brief.get("approved_by_user") is not True:
        raise RuntimeError("Telegram materialized approved brief lacks explicit approval")
    if str(brief.get("control_request_sha256") or "").strip().lower() != request_sha:
        raise RuntimeError("Telegram materialized approved brief is bound to a different request SHA")
    if str(brief.get("control_request_id") or "").strip() != str(request.get("request_id") or "").strip():
        raise RuntimeError("Telegram materialized approved brief is bound to a different request id")

    semantic_hash = str(brief.get("approved_hash") or "").strip().lower()
    _validate_expected_sha256(semantic_hash)
    from isco_video_agent.brief_approval_binding import verify_brief_approval

    verify_brief_approval(brief, semantic_hash)
    return payload


def _restore_committed_approved_brief_worktree(engine_root: Path, committed: bytes) -> None:
    """Restore the tracked compatibility fixture exactly to Engine HEAD before providers."""
    engine = engine_root.resolve()
    restored = checkpoint._run(
        ["git", "checkout", "--", _COMMITTED_BRIEF_PATH],
        cwd=engine,
    )
    if restored.returncode != 0:
        raise RuntimeError("could not restore tracked Engine approved brief after Telegram snapshot")
    path = engine / _COMMITTED_BRIEF_PATH
    if not path.is_file() or path.read_bytes() != committed:
        raise RuntimeError("tracked Engine approved brief was not restored exactly to pinned commit")


def _persist_snapshot_env(
    path: Path,
    snapshot_sha256: str,
    *,
    persist_workflow_env: bool,
) -> None:
    """Bind snapshot identity in-process and optionally publish it to later Actions steps.

    ISCO_APPROVED_BRIEF_SHA256 belongs to Engine brief approval and is computed from
    canonical JSON with hash metadata excluded. Snapshot/checkpoint integrity instead
    binds the exact bytes from the pinned Engine commit or the exact Telegram-approved
    runtime brief, so it has its own raw-byte SHA.

    Low-level materialization is intentionally process-local by default. Only the
    application-owned bootstrap may publish snapshot fixture state through GITHUB_ENV;
    this prevents unit/regression simulations of canonical runtime from contaminating
    later CI steps with temporary snapshot paths.
    """
    expected = _validate_expected_sha256(snapshot_sha256)
    os.environ[_SNAPSHOT_ENV] = str(path)
    os.environ[_SNAPSHOT_SHA_ENV] = expected
    if not persist_workflow_env:
        return
    github_env = str(os.environ.get("GITHUB_ENV") or "").strip()
    if not github_env:
        return
    with Path(github_env).open("a", encoding="utf-8") as handle:
        handle.write(f"{_SNAPSHOT_ENV}={path}\n")
        handle.write(f"{_SNAPSHOT_SHA_ENV}={expected}\n")


def _worktree_brief_drift(engine_root: Path, expected_sha256: str) -> bool:
    path = engine_root.resolve() / _COMMITTED_BRIEF_PATH
    if not path.is_file():
        return True
    return not hmac.compare_digest(_sha256_file(path), expected_sha256)


def materialize_runtime_snapshot(
    repo_root: Path,
    engine_root: Path,
    *,
    persist_workflow_env: bool = False,
) -> Path:
    """Create the run snapshot from the authoritative approved bytes for this ingress.

    Manual V4 keeps the original contract: snapshot the approved brief from pinned
    Engine HEAD, never a mutable worktree. Telegram V4 has a different authoritative
    input: the one-time request materialized and semantically bound immediately after
    authorization consumption. That brief is first validated against the exact request
    id/SHA, copied to the read-only runtime snapshot, and the tracked Engine fixture is
    then restored to HEAD before any provider work. The hermeticity gate remains strict;
    no tracked-source exception is introduced.
    """
    del repo_root
    if not canonical_runtime_enabled():
        raise RuntimeError("immutable planning snapshot is canonical-runtime only")

    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise RuntimeError("RUNNER_TEMP is required for immutable approved brief snapshot")
    destination = Path(temp).resolve() / "isco-state" / _SNAPSHOT_FILENAME

    committed = _committed_approved_brief_bytes(engine_root)
    telegram_payload = _telegram_runtime_approved_brief_bytes(engine_root)
    source = telegram_payload if telegram_payload is not None else committed
    expected = _validate_expected_sha256(_sha256_bytes(source))

    if destination.is_file():
        # One-copy rule: never refresh a snapshot after the run has started.
        if destination.stat().st_mode & 0o222:
            raise RuntimeError("existing approved brief snapshot is writable")
        actual = _sha256_file(destination)
        if not hmac.compare_digest(actual, expected):
            raise RuntimeError("existing approved brief snapshot no longer matches authoritative approved bytes")
        snapshot = destination
    else:
        snapshot = snapshot_approved_brief_bytes(
            source,
            destination,
            expected_sha256=expected,
        )

    _persist_snapshot_env(
        snapshot,
        expected,
        persist_workflow_env=persist_workflow_env,
    )

    committed_expected = _validate_expected_sha256(_sha256_bytes(committed))
    if telegram_payload is not None:
        _restore_committed_approved_brief_worktree(engine_root, committed)
        if _worktree_brief_drift(engine_root, committed_expected):
            raise RuntimeError("Engine approved brief remained non-hermetic after Telegram runtime snapshot")
        print(
            "TELEGRAM_APPROVED_BRIEF_RUNTIME_SNAPSHOT source=authorized_request "
            "engine_worktree_restored=true production_source=runtime_snapshot"
        )
    elif _worktree_brief_drift(engine_root, committed_expected):
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


def _snapshot_expected_sha256() -> str:
    raw = str(os.environ.get(_SNAPSHOT_SHA_ENV) or "").strip().lower()
    if not raw:
        raise RuntimeError(f"{_SNAPSHOT_SHA_ENV} is required for canonical durable planning")
    return _validate_expected_sha256(raw)


def bind_runtime_approved_brief_path() -> Path:
    """Make every in-process Engine reader consume the immutable run snapshot."""
    path = _snapshot_path()
    expected = _snapshot_expected_sha256()
    if not hmac.compare_digest(_sha256_file(path), expected):
        raise RuntimeError("immutable approved brief snapshot does not match pinned approved bytes")
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
    if not force and not canonical_runtime_enabled():
        return

    bind_runtime_approved_brief_path()

    def build_snapshot_binding(repo_root: Path, engine_root: Path) -> checkpoint.Binding:
        brief = bind_runtime_approved_brief_path()
        expected_brief = _snapshot_expected_sha256()
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
    if not canonical_runtime_enabled():
        return checkpoint.RestoreStatus(True, False, "disabled", "non-canonical runtime")
    if not str(encryption_key or "").strip():
        raise RuntimeError("STATE_ENCRYPTION_KEY is required for durable planning checkpoint bootstrap")

    materialize_runtime_snapshot(
        repo_root,
        engine_root,
        persist_workflow_env=True,
    )
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
