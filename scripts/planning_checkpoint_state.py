from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

try:
    from scripts.persistent_memory_core import (
        _atomic_bytes_write,
        _atomic_json_write,
        _coerce_positive_int,
        _git_value,
        _is_commit_sha,
        _raw_commit_parent,
        _run,
    )
    from scripts.persistent_memory_crypto import metadata_from_values, open_envelope, seal
except ModuleNotFoundError:  # direct script/package compatibility
    from persistent_memory_core import (
        _atomic_bytes_write,
        _atomic_json_write,
        _coerce_positive_int,
        _git_value,
        _is_commit_sha,
        _raw_commit_parent,
        _run,
    )
    from persistent_memory_crypto import metadata_from_values, open_envelope, seal


STATE_BRANCH = "planning-state"
STATE_PATH = Path("state/planning-checkpoint.json.enc")
WRAPPER_SCHEMA_VERSION = 1
KIND = "isco-planning-checkpoint"
EMPTY_CHECKPOINT = {"version": 1, "responses": {}}

# Exact Runner-side planner contract. Engine semantics are bound independently by its
# pinned commit SHA. Any change to one of these files invalidates old resume data.
PLANNING_CONTRACT_FILES = (
    "scripts/task_level_planner_router.py",
    "scripts/planning_batch_hardening.py",
    "scripts/provider_capacity_hardening.py",
    "scripts/provider_failure.py",
    "scripts/run124_terminal_provider_recovery.py",
    "scripts/run125_capacity_routing_closure.py",
    "scripts/run125_cache_prefix_contract.py",
    "scripts/schema_repair_policy.py",
    "scripts/planner_quality_guard.py",
    "scripts/run120_dossier_repair_hardening.py",
    "scripts/run120_schema_policy_bridge.py",
    "scripts/attempt9_schema_normalizer.py",
    "scripts/append_retry_guard.py",
)

_CANONICAL_WORKFLOW_MARKER = "/.github/workflows/produce-resilient-v4.yml@"
_RUNTIME_STATE_DIRNAME = "isco-state"
_RUNTIME_KEY_FILENAME = "planning-checkpoint-key"
_RUNTIME_TOKEN_FILENAME = "planning-checkpoint-github-token"
_RUNTIME_IDENTITY_FILENAME = "planning-checkpoint-identity.json"


@dataclass(frozen=True)
class Binding:
    approved_brief_sha256: str
    engine_sha: str
    planning_contract_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "approved_brief_sha256": self.approved_brief_sha256,
            "engine_sha": self.engine_sha,
            "planning_contract_sha256": self.planning_contract_sha256,
        }


@dataclass(frozen=True)
class RestoreStatus:
    persist_allowed: bool
    resume_allowed: bool
    source: str
    reason: str
    state_commit: str = "none"
    state_sequence: int | None = None


@dataclass(frozen=True)
class PersistStatus:
    pushed: bool
    changed: bool
    reason: str


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _canonical_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def planning_contract_sha256(contract_root: Path) -> str:
    entries: list[tuple[str, str]] = []
    root = contract_root.resolve()
    for relative in PLANNING_CONTRACT_FILES:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"planning contract file missing: {relative}")
        entries.append((relative, _sha256_file(path)))
    serialized = "\n".join(f"{name}\0{digest}" for name, digest in entries).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def build_binding(
    *,
    brief_path: Path,
    expected_brief_sha256: str,
    engine_repo: Path,
    expected_engine_sha: str,
    contract_root: Path,
) -> Binding:
    expected_brief = str(expected_brief_sha256 or "").strip().lower()
    expected_engine = str(expected_engine_sha or "").strip().lower()
    if not _valid_sha256(expected_brief):
        raise ValueError("approved brief SHA256 is invalid")
    if not _is_commit_sha(expected_engine):
        raise ValueError("Engine SHA is invalid")

    actual_brief = _sha256_file(brief_path)
    if not hmac.compare_digest(actual_brief, expected_brief):
        raise ValueError("approved brief bytes do not match the pinned SHA256")

    engine_head = _git_value(_run, ["git", "rev-parse", "HEAD"], cwd=engine_repo.resolve())
    if not engine_head or not hmac.compare_digest(engine_head.lower(), expected_engine):
        raise ValueError("Engine checkout does not match the pinned SHA")

    return Binding(expected_brief, expected_engine, planning_contract_sha256(contract_root))


def binding_digest(binding: Binding) -> str:
    return hashlib.sha256(_canonical_bytes(binding.as_dict())).hexdigest()


def _bindings_match(value: object, expected: Binding) -> bool:
    if not isinstance(value, dict) or set(value) != set(expected.as_dict()):
        return False
    return all(
        isinstance(value.get(key), str)
        and hmac.compare_digest(str(value[key]).lower(), digest)
        for key, digest in expected.as_dict().items()
    )


def _normalize_checkpoint(value: object) -> dict:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("planning checkpoint version/root is invalid")
    responses = value.get("responses")
    if not isinstance(responses, dict):
        raise ValueError("planning checkpoint responses must be an object")
    for key, response in responses.items():
        if not isinstance(key, str) or not _valid_sha256(key) or not isinstance(response, dict):
            raise ValueError("planning checkpoint contains an invalid cached response")
    normalized = dict(value)
    normalized["version"] = 1
    normalized["responses"] = dict(responses)
    return normalized


def _write_empty(path: Path) -> None:
    _atomic_json_write(path, dict(EMPTY_CHECKPOINT))


def _identity(status: RestoreStatus, binding: Binding) -> dict:
    return {
        "schema_version": 1,
        "persist_allowed": status.persist_allowed,
        "resume_allowed": status.resume_allowed,
        "source": status.source,
        "reason": status.reason,
        "state_commit": status.state_commit,
        "state_sequence": status.state_sequence,
        "binding_digest": binding_digest(binding),
    }


def _write_identity(path: Path, status: RestoreStatus, binding: Binding) -> None:
    _atomic_json_write(path, _identity(status, binding))


def _read_identity(path: Path, binding: Binding) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("persist_allowed") is not True:
        raise ValueError("planning checkpoint restore identity is not persist-safe")
    actual = str(data.get("binding_digest") or "").lower()
    if not _valid_sha256(actual) or not hmac.compare_digest(actual, binding_digest(binding)):
        raise ValueError("planning checkpoint restore identity binding mismatch")
    commit = str(data.get("state_commit") or "").strip().lower()
    if commit != "none" and not _is_commit_sha(commit):
        raise ValueError("planning checkpoint restore identity has invalid state_commit")
    return data


def _decode(payload: bytes, key: str) -> tuple[dict, object]:
    plaintext, metadata = open_envelope(payload, key)
    try:
        wrapper = json.loads(plaintext.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("authenticated planning checkpoint is invalid JSON") from exc
    if (
        not isinstance(wrapper, dict)
        or wrapper.get("schema_version") != WRAPPER_SCHEMA_VERSION
        or wrapper.get("kind") != KIND
        or wrapper.get("status") not in {"in_progress", "complete"}
    ):
        raise ValueError("authenticated planning checkpoint contract is invalid")
    return wrapper, metadata


def _subject_sequence(subject: str | None) -> int | None:
    prefix = "Update authenticated planning checkpoint (run "
    if not subject or not subject.startswith(prefix) or not subject.endswith(")"):
        return None
    return _coerce_positive_int(subject[len(prefix):-1])


def restore_from_git(
    repo_dir: Path,
    plain_path: Path,
    identity_path: Path,
    key: str,
    binding: Binding,
    *,
    current_run_number: str | None,
    branch: str = STATE_BRANCH,
) -> RestoreStatus:
    repo = repo_dir.resolve()
    ref = f"refs/heads/{branch}"
    probe = _run(["git", "ls-remote", "--exit-code", "--heads", "origin", ref], cwd=repo)

    if probe.returncode == 2:
        _write_empty(plain_path)
        status = RestoreStatus(True, False, "empty", "no durable planning checkpoint")
        _write_identity(identity_path, status, binding)
        return status
    if probe.returncode != 0 or _run(["git", "fetch", "--depth=1", "origin", ref], cwd=repo).returncode != 0:
        _write_empty(plain_path)
        status = RestoreStatus(False, False, branch, "could not fetch authenticated planning-state")
        _write_identity(identity_path, status, binding)
        return status

    shown = _run(["git", "show", f"FETCH_HEAD:{STATE_PATH.as_posix()}"], cwd=repo)
    commit = _git_value(_run, ["git", "rev-parse", "FETCH_HEAD"], cwd=repo)
    parent = _raw_commit_parent(_run, cwd=repo)
    subject = _git_value(_run, ["git", "show", "-s", "--format=%s", "FETCH_HEAD"], cwd=repo)
    if shown.returncode != 0 or not shown.stdout or not commit or not _is_commit_sha(commit.lower()) or parent is None:
        _write_empty(plain_path)
        status = RestoreStatus(False, False, branch, "could not resolve planning-state identity")
        _write_identity(identity_path, status, binding)
        return status

    try:
        wrapper, metadata = _decode(shown.stdout, key)
        if _subject_sequence(subject) != metadata.sequence:
            raise ValueError("planning checkpoint sequence does not match commit subject")
        if metadata.previous_state_commit != parent:
            raise ValueError("planning checkpoint ancestry does not match Git parent")
        current = _coerce_positive_int(current_run_number) if current_run_number else None
        if current is not None and metadata.sequence >= current:
            raise ValueError("planning checkpoint sequence is not older than current run")
    except (OSError, ValueError) as exc:
        _write_empty(plain_path)
        status = RestoreStatus(False, False, branch, str(exc)[:500], commit.lower())
        _write_identity(identity_path, status, binding)
        return status

    if not _bindings_match(wrapper.get("binding"), binding):
        _write_empty(plain_path)
        status = RestoreStatus(
            True, False, branch, "authenticated binding mismatch; starting fresh", commit.lower(), metadata.sequence
        )
        _write_identity(identity_path, status, binding)
        return status
    if wrapper["status"] == "complete":
        _write_empty(plain_path)
        status = RestoreStatus(
            True, False, branch, "authenticated checkpoint is complete; starting fresh", commit.lower(), metadata.sequence
        )
        _write_identity(identity_path, status, binding)
        return status

    checkpoint = _normalize_checkpoint(wrapper.get("checkpoint"))
    _atomic_json_write(plain_path, checkpoint)
    status = RestoreStatus(
        True,
        True,
        branch,
        f"authenticated checkpoint resumed responses={len(checkpoint['responses'])}",
        commit.lower(),
        metadata.sequence,
    )
    _write_identity(identity_path, status, binding)
    return status


def _encrypt(
    plain_path: Path,
    identity_path: Path,
    encrypted_path: Path,
    key: str,
    binding: Binding,
    *,
    run_number: str,
    status: str,
) -> bool:
    identity = _read_identity(identity_path, binding)
    checkpoint = dict(EMPTY_CHECKPOINT)
    if status == "in_progress":
        if not plain_path.is_file():
            return False
        checkpoint = _normalize_checkpoint(json.loads(plain_path.read_text(encoding="utf-8")))
        if not checkpoint["responses"]:
            return False
    wrapper = {
        "schema_version": WRAPPER_SCHEMA_VERSION,
        "kind": KIND,
        "status": status,
        "binding": binding.as_dict(),
        "checkpoint": checkpoint,
    }
    metadata = metadata_from_values(
        run_number=run_number,
        previous_state_commit=str(identity.get("state_commit") or "none").strip().lower(),
    )
    plaintext = _canonical_bytes(wrapper)
    payload = seal(plaintext, key, metadata=metadata)
    verified_plain, verified_meta = open_envelope(payload, key)
    if verified_plain != plaintext or verified_meta != metadata:
        raise RuntimeError("planning checkpoint AES-GCM self-verification failed")
    _atomic_bytes_write(encrypted_path, payload)
    return True


def _persist_encrypted(
    repo_dir: Path,
    encrypted_path: Path,
    key: str,
    *,
    run_number: str,
    branch: str,
    run_cmd=_run,
) -> PersistStatus:
    repo = repo_dir.resolve()
    _wrapper, metadata = _decode(encrypted_path.read_bytes(), key)
    expected_run = _coerce_positive_int(run_number)
    if expected_run is None or metadata.sequence != expected_run:
        raise ValueError("planning checkpoint run sequence does not match this run")

    ref = f"refs/heads/{branch}"
    probe = run_cmd(["git", "ls-remote", "--exit-code", "--heads", "origin", ref], cwd=repo)
    if probe.returncode == 0:
        if run_cmd(["git", "fetch", "--depth=1", "origin", ref], cwd=repo).returncode != 0:
            return PersistStatus(False, False, "could not fetch current planning-state")
        current = _git_value(run_cmd, ["git", "rev-parse", "FETCH_HEAD"], cwd=repo)
        if not current or metadata.previous_state_commit != current.lower():
            return PersistStatus(False, False, "authenticated planning-state ancestry is stale")
        if run_cmd(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=repo).returncode != 0:
            return PersistStatus(False, False, "could not checkout planning-state")
    elif probe.returncode == 2:
        if metadata.previous_state_commit != "none":
            return PersistStatus(False, False, "planning-state unexpectedly absent")
        if run_cmd(["git", "checkout", "--orphan", branch], cwd=repo).returncode != 0:
            return PersistStatus(False, False, "could not create planning-state")
        if run_cmd(["git", "rm", "-rf", "--ignore-unmatch", "."], cwd=repo).returncode != 0:
            return PersistStatus(False, False, "could not clean planning-state")
    else:
        return PersistStatus(False, False, "could not probe planning-state")

    target = repo / STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(encrypted_path, target)
    run_cmd(["git", "config", "user.name", "github-actions[bot]"], cwd=repo)
    run_cmd(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo)
    if run_cmd(["git", "add", STATE_PATH.as_posix()], cwd=repo).returncode != 0:
        return PersistStatus(False, False, "could not stage planning checkpoint")
    diff = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo)
    if diff.returncode == 0:
        return PersistStatus(True, False, "authenticated planning checkpoint unchanged")
    if diff.returncode != 1:
        return PersistStatus(False, False, "could not inspect planning checkpoint diff")
    if run_cmd(
        ["git", "commit", "-m", f"Update authenticated planning checkpoint (run {expected_run})"], cwd=repo
    ).returncode != 0:
        return PersistStatus(False, False, "could not commit planning checkpoint")
    pushed = run_cmd(["git", "push", "origin", f"HEAD:{ref}"], cwd=repo)
    if pushed.returncode != 0:
        return PersistStatus(False, True, "planning-state push rejected")
    return PersistStatus(True, True, "authenticated planning checkpoint updated")


def persist_checkpoint(
    *,
    repo_dir: Path,
    plain_path: Path,
    identity_path: Path,
    encrypted_path: Path,
    key: str,
    binding: Binding,
    run_number: str,
    status: str,
    branch: str = STATE_BRANCH,
    run_cmd=_run,
) -> PersistStatus:
    if status not in {"in_progress", "complete"}:
        raise ValueError("planning checkpoint status is invalid")
    if not _encrypt(
        plain_path,
        identity_path,
        encrypted_path,
        key,
        binding,
        run_number=run_number,
        status=status,
    ):
        return PersistStatus(True, False, "no non-empty in-progress checkpoint to persist")
    return _persist_encrypted(
        repo_dir,
        encrypted_path,
        key,
        run_number=run_number,
        branch=branch,
        run_cmd=run_cmd,
    )


def canonical_runtime_enabled() -> bool:
    return (
        str(os.environ.get("GITHUB_ACTIONS") or "").strip().lower() == "true"
        and str(os.environ.get("GITHUB_EVENT_NAME") or "").strip() == "workflow_dispatch"
        and _CANONICAL_WORKFLOW_MARKER in str(os.environ.get("GITHUB_WORKFLOW_REF") or "")
    )


def _state_dir() -> Path:
    temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not temp:
        raise RuntimeError("RUNNER_TEMP is required for durable planning checkpoint")
    path = Path(temp) / _RUNTIME_STATE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _write_secret(path: Path, value: str) -> None:
    if not value:
        raise ValueError(f"empty runtime secret: {path.name}")
    _atomic_bytes_write(path, value.encode("utf-8"))
    path.chmod(0o600)


def _read_secret(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"runtime secret is empty: {path.name}")
    return value


def runtime_identity_path() -> Path:
    return _state_dir() / _RUNTIME_IDENTITY_FILENAME


def runtime_plain_path(engine_root: Path) -> Path:
    return engine_root.resolve() / "state" / "planning-checkpoint.json"


def build_runtime_binding(repo_root: Path, engine_root: Path) -> Binding:
    brief = engine_root.resolve() / "production" / "approved_brief.json"
    actual_brief = _sha256_file(brief)
    engine_head = _git_value(_run, ["git", "rev-parse", "HEAD"], cwd=engine_root.resolve())
    if not engine_head:
        raise ValueError("could not resolve Engine checkout")
    return build_binding(
        brief_path=brief,
        expected_brief_sha256=str(os.environ.get("ISCO_APPROVED_BRIEF_SHA256") or actual_brief),
        engine_repo=engine_root,
        expected_engine_sha=str(os.environ.get("ISCO_ENGINE_SHA") or engine_head),
        contract_root=repo_root,
    )


def bootstrap_runtime_restore(*, repo_root: Path, engine_root: Path, key: str) -> RestoreStatus:
    if not canonical_runtime_enabled():
        return RestoreStatus(True, False, "disabled", "non-canonical runtime")
    binding = build_runtime_binding(repo_root, engine_root)
    status = restore_from_git(
        repo_root,
        runtime_plain_path(engine_root),
        runtime_identity_path(),
        key,
        binding,
        current_run_number=os.environ.get("GITHUB_RUN_NUMBER"),
    )
    if not status.persist_allowed:
        raise RuntimeError("durable planning checkpoint authentication/lineage failed: " + status.reason)
    _write_secret(_state_dir() / _RUNTIME_KEY_FILENAME, key)
    print(
        "Durable planning checkpoint bootstrap PASS: "
        f"resume_allowed={status.resume_allowed} sequence={status.state_sequence or 'none'}"
    )
    return status


def materialize_runtime_github_token(token: str) -> None:
    if canonical_runtime_enabled():
        _write_secret(_state_dir() / _RUNTIME_TOKEN_FILENAME, token)


def _authenticated_run(token: str):
    basic = base64.b64encode(("x-access-token:" + token).encode("utf-8")).decode("ascii")

    def run_cmd(
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess:
        child = dict(os.environ if env is None else env)
        child["GIT_CONFIG_COUNT"] = "1"
        child["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        child["GIT_CONFIG_VALUE_0"] = "AUTHORIZATION: basic " + basic
        return _run(args, cwd=cwd, env=child, input_bytes=input_bytes)

    return run_cmd


def persist_runtime_checkpoint(*, repo_root: Path, engine_root: Path, status: str) -> PersistStatus:
    if not canonical_runtime_enabled():
        return PersistStatus(True, False, "disabled outside canonical runtime")
    key = _read_secret(_state_dir() / _RUNTIME_KEY_FILENAME)
    token = _read_secret(_state_dir() / _RUNTIME_TOKEN_FILENAME)
    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    run_number = str(os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    if not repository or _coerce_positive_int(run_number) is None:
        raise RuntimeError("GitHub run identity is missing")

    with tempfile.TemporaryDirectory(prefix="isco-planning-writer-") as td:
        writer = Path(td) / "repo"
        clone = _run(["git", "clone", "--depth=1", f"https://github.com/{repository}.git", str(writer)])
        if clone.returncode != 0:
            raise RuntimeError("could not create planning-state writer clone")
        result = persist_checkpoint(
            repo_dir=writer,
            plain_path=runtime_plain_path(engine_root),
            identity_path=runtime_identity_path(),
            encrypted_path=_state_dir() / "planning-checkpoint.json.enc",
            key=key,
            binding=build_runtime_binding(repo_root, engine_root),
            run_number=run_number,
            status=status,
            run_cmd=_authenticated_run(token),
        )
    if not result.pushed:
        raise RuntimeError("durable planning checkpoint persistence failed: " + result.reason)
    return result


def install_runtime_persistence_wrapper(orchestrator_module) -> None:
    if not canonical_runtime_enabled():
        return
    original = orchestrator_module.produce
    if getattr(original, "_isco_durable_planning_checkpoint", False):
        return

    repo_root = Path(__file__).resolve().parents[1]
    engine_root = Path.cwd().resolve()

    def wrapped(*args, **kwargs):
        try:
            result = original(*args, **kwargs)
        except BaseException:
            try:
                saved = persist_runtime_checkpoint(
                    repo_root=repo_root, engine_root=engine_root, status="in_progress"
                )
                print("Durable planning checkpoint failure-save:", saved.reason)
            except Exception as exc:
                # Preserve the real production exception; surface checkpoint failure beside it.
                print(
                    "DURABLE_PLANNING_CHECKPOINT_SAVE_FAILED "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            raise

        saved = persist_runtime_checkpoint(repo_root=repo_root, engine_root=engine_root, status="complete")
        print("Durable planning checkpoint completion marker:", saved.reason)
        return result

    wrapped._isco_durable_planning_checkpoint = True
    wrapped._isco_durable_planning_checkpoint_original = original
    orchestrator_module.produce = wrapped
