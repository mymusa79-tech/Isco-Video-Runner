from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

try:
    from scripts.persistent_memory_crypto import (
        EnvelopeMetadata,
        is_authenticated_v2,
        metadata_from_values,
        open_envelope,
        parse_envelope,
        seal,
    )
except ModuleNotFoundError:  # direct `python scripts/persistent_memory.py`
    from persistent_memory_crypto import (
        EnvelopeMetadata,
        is_authenticated_v2,
        metadata_from_values,
        open_envelope,
        parse_envelope,
        seal,
    )


STATE_BRANCH = "agent-state"
STATE_PATH = Path("state/history.json.enc")
LEGACY_STATE_PATH = Path("state/history.json.enc")
EMPTY_HISTORY = {"videos": []}
OPENSSL_CIPHER = "-aes-256-cbc"

# One-time migration pins. CBC is accepted only when Git proves the encrypted blob is
# exactly one of the pre-hardening blobs known at this change. No arbitrary legacy
# payload is ever decrypted after this hardening lands.
LEGACY_AGENT_STATE_BLOB_SHA = "eb5c9eb8dd9036b5846dfb38b32ca4500867865b"
LEGACY_MAIN_BLOB_SHA = "f9606e5127d4e7d90d37910f5ab3a27216d6a6e0"
APPROVED_LEGACY_BLOB_SHAS = frozenset({LEGACY_AGENT_STATE_BLOB_SHA, LEGACY_MAIN_BLOB_SHA})


@dataclass(frozen=True)
class RestoreStatus:
    save_allowed: bool
    source: str
    reason: str = ""
    state_commit: str = "none"
    state_sequence: int | None = None
    previous_state_commit: str = "none"


@dataclass(frozen=True)
class PersistStatus:
    pushed: bool
    changed: bool
    reason: str = ""


RunCommand = Callable[..., subprocess.CompletedProcess]


def _run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        cwd=str(cwd) if cwd else None,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _visual_asset_pexels_id(asset: object) -> int | None:
    if isinstance(asset, (int, str)):
        return _coerce_positive_int(asset)
    if not isinstance(asset, dict):
        return None
    provider = str(asset.get("provider", "")).strip().lower()
    if provider and provider != "pexels":
        return None
    for key in ("pexels_id", "id", "asset_id", "source_id"):
        value = _coerce_positive_int(asset.get(key))
        if value is not None:
            return value
    return None


def normalize_record(record: dict) -> dict:
    normalized = dict(record)
    ids: set[int] = set()
    legacy_ids = record.get("pexels_ids", [])
    if isinstance(legacy_ids, list):
        for value in legacy_ids:
            parsed = _coerce_positive_int(value)
            if parsed is not None:
                ids.add(parsed)
    original_assets = record.get("visual_assets", [])
    assets: list[object] = list(original_assets) if isinstance(original_assets, list) else []
    represented_pexels: set[int] = set()
    for asset in assets:
        parsed = _visual_asset_pexels_id(asset)
        if parsed is not None:
            ids.add(parsed)
            represented_pexels.add(parsed)
    for pexels_id in sorted(ids - represented_pexels):
        assets.append({"provider": "pexels", "id": pexels_id})
    normalized["pexels_ids"] = sorted(ids)
    if assets or "visual_assets" in record or ids:
        normalized["visual_assets"] = assets
    return normalized


def normalize_history(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("history root must be a JSON object")
    videos = data.get("videos", [])
    if not isinstance(videos, list):
        raise ValueError("history.videos must be a JSON array")
    normalized = dict(data)
    normalized["videos"] = [normalize_record(item) for item in videos if isinstance(item, dict)]
    return normalized


def _write_locked_empty_history(plain_path: Path) -> None:
    _atomic_json_write(plain_path, EMPTY_HISTORY)


def _legacy_decrypt(
    encrypted_path: Path,
    key: str,
    *,
    run_cmd: RunCommand,
) -> bytes:
    with tempfile.TemporaryDirectory(prefix="isco-legacy-state-") as td:
        tmp = Path(td) / "history.json"
        env = dict(os.environ)
        env["STATE_ENCRYPTION_KEY"] = key
        result = run_cmd(
            [
                "openssl",
                "enc",
                "-d",
                OPENSSL_CIPHER,
                "-pbkdf2",
                "-in",
                str(encrypted_path),
                "-out",
                str(tmp),
                "-pass",
                "env:STATE_ENCRYPTION_KEY",
            ],
            env=env,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
            suffix = detail[-1] if detail else "openssl decrypt failed"
            raise ValueError(suffix)
        return tmp.read_bytes()


def _normalize_plain_bytes(plaintext: bytes) -> dict:
    try:
        data = json.loads(plaintext.decode("utf-8"))
        return normalize_history(data)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid decrypted history: {exc}") from exc


def decrypt_history(
    encrypted_path: Path,
    plain_path: Path,
    key: str,
    *,
    run_cmd: RunCommand = _run,
    legacy_blob_sha: str | None = None,
) -> RestoreStatus:
    if not key:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", "STATE_ENCRYPTION_KEY is missing")
    if not encrypted_path.is_file() or encrypted_path.stat().st_size == 0:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", "encrypted history is missing or empty")

    payload = encrypted_path.read_bytes()
    try:
        if is_authenticated_v2(payload):
            plaintext, metadata = open_envelope(payload, key)
            normalized = _normalize_plain_bytes(plaintext)
            _atomic_json_write(plain_path, normalized)
            return RestoreStatus(
                True,
                "encrypted-v2",
                "authenticated AES-256-GCM state",
                state_sequence=metadata.sequence,
                previous_state_commit=metadata.previous_state_commit,
            )

        blob_sha = str(legacy_blob_sha or "").strip().lower()
        if not payload.startswith(b"Salted__") or blob_sha not in APPROVED_LEGACY_BLOB_SHAS:
            raise ValueError("legacy unauthenticated state is not an approved one-time migration blob")
        plaintext = _legacy_decrypt(encrypted_path, key, run_cmd=run_cmd)
        normalized = _normalize_plain_bytes(plaintext)
        _atomic_json_write(plain_path, normalized)
        return RestoreStatus(
            True,
            "encrypted-legacy-migration",
            "pinned legacy CBC state accepted for one-time authenticated migration",
        )
    except (OSError, ValueError) as exc:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", str(exc)[:500])


def _completed_text(result: subprocess.CompletedProcess) -> str:
    return result.stdout.decode("utf-8", errors="replace").strip()


def _git_value(run_cmd: RunCommand, args: list[str], *, cwd: Path) -> str | None:
    result = run_cmd(args, cwd=cwd)
    if result.returncode != 0:
        return None
    value = _completed_text(result)
    return value or None


def _commit_subject_sequence(subject: str | None) -> int | None:
    if not subject:
        return None
    prefix = "Update authenticated agent state (run "
    if not subject.startswith(prefix) or not subject.endswith(")"):
        return None
    return _coerce_positive_int(subject[len(prefix) : -1])


def restore_from_git(
    repo_dir: Path,
    plain_path: Path,
    key: str,
    *,
    branch: str = STATE_BRANCH,
    state_path: Path = STATE_PATH,
    legacy_state_path: Path = LEGACY_STATE_PATH,
    current_run_number: str | None = None,
    run_cmd: RunCommand = _run,
) -> RestoreStatus:
    repo_dir = repo_dir.resolve()
    ref = f"refs/heads/{branch}"
    probe = run_cmd(["git", "ls-remote", "--exit-code", "--heads", "origin", ref], cwd=repo_dir)

    if probe.returncode == 0:
        fetched = run_cmd(["git", "fetch", "--depth=1", "origin", ref], cwd=repo_dir)
        if fetched.returncode != 0:
            _write_locked_empty_history(plain_path)
            return RestoreStatus(False, "agent-state", "could not fetch agent-state")
        shown = run_cmd(["git", "show", f"FETCH_HEAD:{state_path.as_posix()}"], cwd=repo_dir)
        if shown.returncode != 0 or not shown.stdout:
            _write_locked_empty_history(plain_path)
            return RestoreStatus(False, "agent-state", "agent-state exists but encrypted history is missing")
        commit_sha = _git_value(run_cmd, ["git", "rev-parse", "FETCH_HEAD"], cwd=repo_dir)
        blob_sha = _git_value(run_cmd, ["git", "rev-parse", f"FETCH_HEAD:{state_path.as_posix()}"], cwd=repo_dir)
        parent_line = _git_value(run_cmd, ["git", "show", "-s", "--format=%P", "FETCH_HEAD"], cwd=repo_dir)
        subject = _git_value(run_cmd, ["git", "show", "-s", "--format=%s", "FETCH_HEAD"], cwd=repo_dir)
        if not commit_sha or not blob_sha:
            _write_locked_empty_history(plain_path)
            return RestoreStatus(False, "agent-state", "could not resolve agent-state identity")
        encrypted = plain_path.parent / "history.from-agent-state.enc"
        _atomic_bytes_write(encrypted, shown.stdout)
        status = decrypt_history(encrypted, plain_path, key, run_cmd=run_cmd, legacy_blob_sha=blob_sha)
        if not status.save_allowed:
            return RestoreStatus(False, "agent-state", status.reason, state_commit=commit_sha)

        if status.state_sequence is not None:
            authenticated_sequence = status.state_sequence
            subject_sequence = _commit_subject_sequence(subject)
            if subject_sequence != authenticated_sequence:
                _write_locked_empty_history(plain_path)
                return RestoreStatus(False, "agent-state", "authenticated state sequence does not match commit subject", state_commit=commit_sha)
            expected_parent = (parent_line or "none").split()[0] if parent_line else "none"
            if status.previous_state_commit != expected_parent:
                _write_locked_empty_history(plain_path)
                return RestoreStatus(False, "agent-state", "authenticated state ancestry does not match Git parent", state_commit=commit_sha)
            current = _coerce_positive_int(current_run_number) if current_run_number else None
            if current is not None and authenticated_sequence >= current:
                _write_locked_empty_history(plain_path)
                return RestoreStatus(False, "agent-state", "authenticated state sequence is not older than current workflow run", state_commit=commit_sha)

        return RestoreStatus(
            True,
            "agent-state",
            status.reason,
            state_commit=commit_sha,
            state_sequence=status.state_sequence,
            previous_state_commit=status.previous_state_commit,
        )

    if probe.returncode != 2:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "agent-state", f"agent-state probe failed (git rc={probe.returncode})")

    legacy = repo_dir / legacy_state_path
    if legacy.is_file() and legacy.stat().st_size > 0:
        blob_sha = _git_value(run_cmd, ["git", "hash-object", legacy_state_path.as_posix()], cwd=repo_dir)
        status = decrypt_history(legacy, plain_path, key, run_cmd=run_cmd, legacy_blob_sha=blob_sha)
        return RestoreStatus(
            status.save_allowed,
            "legacy-main",
            status.reason,
            state_commit="none",
            state_sequence=status.state_sequence,
            previous_state_commit="none",
        )

    _atomic_json_write(plain_path, EMPTY_HISTORY)
    return RestoreStatus(True, "empty", "", state_commit="none")


def encrypt_history(
    plain_path: Path,
    encrypted_path: Path,
    key: str,
    *,
    run_number: str,
    previous_state_commit: str,
    run_cmd: RunCommand = _run,
) -> None:
    del run_cmd  # retained for API compatibility; v2 uses cryptography, not openssl enc.
    if not key:
        raise ValueError("STATE_ENCRYPTION_KEY is missing")
    data = json.loads(plain_path.read_text(encoding="utf-8"))
    normalized = normalize_history(data)
    plaintext = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    metadata = metadata_from_values(run_number=run_number, previous_state_commit=previous_state_commit)
    payload = seal(plaintext, key, metadata=metadata)
    # Self-verify before making the envelope canonical on disk.
    verified_plaintext, verified_metadata = open_envelope(payload, key)
    if verified_plaintext != plaintext or verified_metadata != metadata:
        raise RuntimeError("authenticated persistent-memory self-verification failed")
    _atomic_bytes_write(encrypted_path, payload)


def should_persist(*, technical_success: bool, approval_decision: str, restore_save_allowed: bool) -> bool:
    return technical_success and restore_save_allowed and approval_decision.strip().lower() == "approved"


def persist_encrypted_state(
    repo_dir: Path,
    encrypted_state: Path,
    *,
    branch: str = STATE_BRANCH,
    run_number: str = "unknown",
    key: str | None = None,
    run_cmd: RunCommand = _run,
) -> PersistStatus:
    repo_dir = repo_dir.resolve()
    payload = encrypted_state.read_bytes()
    try:
        _, metadata = open_envelope(payload, key or "")
    except (OSError, ValueError) as exc:
        raise ValueError(f"refusing to persist unauthenticated state: {exc}") from exc
    expected_run = _coerce_positive_int(run_number)
    if expected_run is None or metadata.sequence != expected_run or metadata.run_number != str(expected_run):
        raise ValueError("refusing to persist state whose authenticated run sequence does not match this run")

    ref = f"refs/heads/{branch}"
    probe = run_cmd(["git", "ls-remote", "--exit-code", "--heads", "origin", ref], cwd=repo_dir)
    if probe.returncode == 0:
        fetched = run_cmd(["git", "fetch", "--depth=1", "origin", ref], cwd=repo_dir)
        if fetched.returncode != 0:
            return PersistStatus(False, False, "warning: could not fetch current agent-state; remote left untouched")
        current_commit = _git_value(run_cmd, ["git", "rev-parse", "FETCH_HEAD"], cwd=repo_dir)
        if not current_commit:
            return PersistStatus(False, False, "warning: could not resolve current agent-state commit; remote left untouched")
        if metadata.previous_state_commit != current_commit:
            return PersistStatus(False, False, "warning: authenticated state ancestry is stale; remote left untouched")
        checkout = run_cmd(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=repo_dir)
        if checkout.returncode != 0:
            return PersistStatus(False, False, "warning: could not checkout current agent-state; remote left untouched")
    elif probe.returncode == 2:
        if metadata.previous_state_commit != "none":
            return PersistStatus(False, False, "warning: authenticated state expects a previous commit but agent-state is absent")
        orphan = run_cmd(["git", "checkout", "--orphan", branch], cwd=repo_dir)
        if orphan.returncode != 0:
            return PersistStatus(False, False, "warning: could not create orphan agent-state; remote left untouched")
        removed = run_cmd(["git", "rm", "-rf", "--ignore-unmatch", "."], cwd=repo_dir)
        if removed.returncode != 0:
            return PersistStatus(False, False, "warning: could not clean orphan agent-state; remote left untouched")
    else:
        return PersistStatus(False, False, f"warning: agent-state probe failed (git rc={probe.returncode}); remote left untouched")

    target = repo_dir / STATE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(encrypted_state, target)
    run_cmd(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_dir)
    run_cmd(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo_dir)
    added = run_cmd(["git", "add", STATE_PATH.as_posix()], cwd=repo_dir)
    if added.returncode != 0:
        return PersistStatus(False, False, "warning: could not stage authenticated state; remote left untouched")
    diff = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
    if diff.returncode == 0:
        return PersistStatus(True, False, "authenticated state unchanged")
    if diff.returncode != 1:
        return PersistStatus(False, False, "warning: could not inspect state diff; remote left untouched")

    commit = run_cmd(["git", "commit", "-m", f"Update authenticated agent state (run {expected_run})"], cwd=repo_dir)
    if commit.returncode != 0:
        return PersistStatus(False, False, "warning: could not commit authenticated state; remote left untouched")
    pushed = run_cmd(["git", "push", "origin", f"HEAD:{ref}"], cwd=repo_dir)
    if pushed.returncode != 0:
        detail = pushed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "push rejected"
        return PersistStatus(False, True, f"warning: agent-state push failed; remote left untouched: {suffix}")
    return PersistStatus(True, True, "authenticated agent-state updated")


def _append_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as fh:
        for key, value in values.items():
            safe = value.replace("\n", " ").replace("\r", " ")
            fh.write(f"{key}={safe}\n")


def _cmd_restore(args: argparse.Namespace) -> int:
    plain = Path(args.plain)
    status = restore_from_git(
        Path(args.repo),
        plain,
        os.environ.get("STATE_ENCRYPTION_KEY", ""),
        branch=args.branch,
        legacy_state_path=Path(args.legacy_state),
        current_run_number=os.environ.get("GITHUB_RUN_NUMBER"),
    )
    _append_output(
        args.github_output,
        {
            "save_allowed": "true" if status.save_allowed else "false",
            "source": status.source,
            "reason": status.reason,
            "plain_path": str(plain),
            "state_commit": status.state_commit,
            "state_sequence": str(status.state_sequence or ""),
        },
    )
    if status.save_allowed:
        print(f"Persistent memory restore OK: source={status.source}; commit={status.state_commit}")
    else:
        print(f"::warning::Persistent memory restore locked saving: source={status.source}; reason={status.reason}")
    return 0


def _cmd_encrypt(args: argparse.Namespace) -> int:
    encrypt_history(
        Path(args.plain),
        Path(args.encrypted),
        os.environ.get("STATE_ENCRYPTION_KEY", ""),
        run_number=args.run_number,
        previous_state_commit=args.previous_state_commit,
    )
    print(f"Authenticated persistent memory: {args.encrypted}")
    return 0


def _cmd_persist(args: argparse.Namespace) -> int:
    status = persist_encrypted_state(
        Path(args.repo),
        Path(args.encrypted),
        branch=args.branch,
        run_number=args.run_number,
        key=os.environ.get("STATE_ENCRYPTION_KEY", ""),
    )
    if status.pushed:
        print(status.reason)
    else:
        print(f"::warning::{status.reason}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authenticated encrypted persistent cross-run memory helpers")
    sub = parser.add_subparsers(dest="command", required=True)

    restore = sub.add_parser("restore")
    restore.add_argument("--repo", default=".")
    restore.add_argument("--plain", required=True)
    restore.add_argument("--branch", default=STATE_BRANCH)
    restore.add_argument("--legacy-state", default=LEGACY_STATE_PATH.as_posix())
    restore.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    restore.set_defaults(func=_cmd_restore)

    encrypt = sub.add_parser("encrypt")
    encrypt.add_argument("--plain", required=True)
    encrypt.add_argument("--encrypted", required=True)
    encrypt.add_argument("--run-number", required=True)
    encrypt.add_argument("--previous-state-commit", required=True)
    encrypt.set_defaults(func=_cmd_encrypt)

    persist = sub.add_parser("persist")
    persist.add_argument("--repo", required=True)
    persist.add_argument("--encrypted", required=True)
    persist.add_argument("--branch", default=STATE_BRANCH)
    persist.add_argument("--run-number", default=os.environ.get("GITHUB_RUN_NUMBER", "unknown"))
    persist.set_defaults(func=_cmd_persist)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
