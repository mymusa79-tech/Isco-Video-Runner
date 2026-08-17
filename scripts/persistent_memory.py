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

STATE_BRANCH = "agent-state"
STATE_PATH = Path("state/history.json.enc")
LEGACY_STATE_PATH = Path("state/history.json.enc")
EMPTY_HISTORY = {"videos": []}
OPENSSL_CIPHER = "-aes-256-cbc"


@dataclass(frozen=True)
class RestoreStatus:
    save_allowed: bool
    source: str
    reason: str = ""


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

    # New storage format is visual_assets. Keep the legacy projection too because the
    # pinned Engine currently consumes pexels_ids directly. This makes restored state
    # readable by both old and new runtimes without discarding non-Pexels assets.
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


def decrypt_history(
    encrypted_path: Path,
    plain_path: Path,
    key: str,
    *,
    run_cmd: RunCommand = _run,
) -> RestoreStatus:
    if not key:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", "STATE_ENCRYPTION_KEY is missing")
    if not encrypted_path.is_file() or encrypted_path.stat().st_size == 0:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", "encrypted history is missing or empty")

    plain_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plain_path.with_name(plain_path.name + ".decrypting")
    tmp.unlink(missing_ok=True)
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
        tmp.unlink(missing_ok=True)
        _write_locked_empty_history(plain_path)
        detail = result.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "openssl decrypt failed"
        return RestoreStatus(False, "encrypted", suffix)

    try:
        data = json.loads(tmp.read_text(encoding="utf-8"))
        normalized = normalize_history(data)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "encrypted", f"invalid decrypted history: {exc}")

    tmp.unlink(missing_ok=True)
    _atomic_json_write(plain_path, normalized)
    return RestoreStatus(True, "encrypted")


def restore_from_git(
    repo_dir: Path,
    plain_path: Path,
    key: str,
    *,
    branch: str = STATE_BRANCH,
    state_path: Path = STATE_PATH,
    legacy_state_path: Path = LEGACY_STATE_PATH,
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
        encrypted = plain_path.parent / "history.from-agent-state.enc"
        encrypted.parent.mkdir(parents=True, exist_ok=True)
        encrypted.write_bytes(shown.stdout)
        status = decrypt_history(encrypted, plain_path, key, run_cmd=run_cmd)
        return RestoreStatus(status.save_allowed, "agent-state", status.reason)

    # git ls-remote --exit-code returns 2 when the remote is reachable but the ref is
    # absent. Any other non-zero status is a transport/auth failure and MUST lock save.
    if probe.returncode != 2:
        _write_locked_empty_history(plain_path)
        return RestoreStatus(False, "agent-state", f"agent-state probe failed (git rc={probe.returncode})")

    legacy = repo_dir / legacy_state_path
    if legacy.is_file() and legacy.stat().st_size > 0:
        status = decrypt_history(legacy, plain_path, key, run_cmd=run_cmd)
        return RestoreStatus(status.save_allowed, "legacy-main", status.reason)

    _atomic_json_write(plain_path, EMPTY_HISTORY)
    return RestoreStatus(True, "empty")


def encrypt_history(
    plain_path: Path,
    encrypted_path: Path,
    key: str,
    *,
    run_cmd: RunCommand = _run,
) -> None:
    if not key:
        raise ValueError("STATE_ENCRYPTION_KEY is missing")
    data = json.loads(plain_path.read_text(encoding="utf-8"))
    normalized = normalize_history(data)

    encrypted_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="isco-state-") as td:
        normalized_path = Path(td) / "history.json"
        normalized_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(normalized_path, stat.S_IRUSR | stat.S_IWUSR)
        tmp_encrypted = Path(td) / "history.json.enc"
        env = dict(os.environ)
        env["STATE_ENCRYPTION_KEY"] = key
        result = run_cmd(
            [
                "openssl",
                "enc",
                OPENSSL_CIPHER,
                "-pbkdf2",
                "-salt",
                "-in",
                str(normalized_path),
                "-out",
                str(tmp_encrypted),
                "-pass",
                "env:STATE_ENCRYPTION_KEY",
            ],
            env=env,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"openssl encrypt failed: {detail}")
        payload = tmp_encrypted.read_bytes()
        if len(payload) < 16 or not payload.startswith(b"Salted__"):
            raise RuntimeError("encrypted state is not an OpenSSL salted payload")
        tmp_target = encrypted_path.with_name(encrypted_path.name + ".tmp")
        tmp_target.write_bytes(payload)
        os.chmod(tmp_target, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_target, encrypted_path)


def should_persist(*, technical_success: bool, approval_decision: str, restore_save_allowed: bool) -> bool:
    return technical_success and restore_save_allowed and approval_decision.strip().lower() == "approved"


def persist_encrypted_state(
    repo_dir: Path,
    encrypted_state: Path,
    *,
    branch: str = STATE_BRANCH,
    run_number: str = "unknown",
    run_cmd: RunCommand = _run,
) -> PersistStatus:
    repo_dir = repo_dir.resolve()
    payload = encrypted_state.read_bytes()
    if len(payload) < 16 or not payload.startswith(b"Salted__"):
        raise ValueError("refusing to persist non-OpenSSL encrypted state")

    ref = f"refs/heads/{branch}"
    probe = run_cmd(["git", "ls-remote", "--exit-code", "--heads", "origin", ref], cwd=repo_dir)
    if probe.returncode == 0:
        fetched = run_cmd(["git", "fetch", "--depth=1", "origin", ref], cwd=repo_dir)
        if fetched.returncode != 0:
            return PersistStatus(False, False, "warning: could not fetch current agent-state; remote left untouched")
        checkout = run_cmd(["git", "checkout", "-B", branch, "FETCH_HEAD"], cwd=repo_dir)
        if checkout.returncode != 0:
            return PersistStatus(False, False, "warning: could not checkout current agent-state; remote left untouched")
    elif probe.returncode == 2:
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
    run_cmd(
        ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
        cwd=repo_dir,
    )
    added = run_cmd(["git", "add", STATE_PATH.as_posix()], cwd=repo_dir)
    if added.returncode != 0:
        return PersistStatus(False, False, "warning: could not stage encrypted state; remote left untouched")
    diff = run_cmd(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
    if diff.returncode == 0:
        return PersistStatus(True, False, "encrypted state unchanged")
    if diff.returncode != 1:
        return PersistStatus(False, False, "warning: could not inspect state diff; remote left untouched")

    commit = run_cmd(
        ["git", "commit", "-m", f"Update encrypted agent state (run {run_number})"],
        cwd=repo_dir,
    )
    if commit.returncode != 0:
        return PersistStatus(False, False, "warning: could not commit encrypted state; remote left untouched")

    # Deliberately ordinary push. Never add --force, --force-with-lease, '+' refspec,
    # or retry after rejection. A concurrent writer wins; this run only warns.
    pushed = run_cmd(["git", "push", "origin", f"HEAD:{ref}"], cwd=repo_dir)
    if pushed.returncode != 0:
        detail = pushed.stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = detail[-1] if detail else "push rejected"
        return PersistStatus(False, True, f"warning: agent-state push failed; remote left untouched: {suffix}")
    return PersistStatus(True, True, "agent-state updated")


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
    )
    _append_output(
        args.github_output,
        {
            "save_allowed": "true" if status.save_allowed else "false",
            "source": status.source,
            "reason": status.reason,
            "plain_path": str(plain),
        },
    )
    if status.save_allowed:
        print(f"Persistent memory restore OK: source={status.source}")
    else:
        print(f"::warning::Persistent memory restore locked saving: source={status.source}; reason={status.reason}")
    return 0


def _cmd_encrypt(args: argparse.Namespace) -> int:
    encrypt_history(
        Path(args.plain),
        Path(args.encrypted),
        os.environ.get("STATE_ENCRYPTION_KEY", ""),
    )
    print(f"Encrypted persistent memory: {args.encrypted}")
    return 0


def _cmd_persist(args: argparse.Namespace) -> int:
    status = persist_encrypted_state(
        Path(args.repo),
        Path(args.encrypted),
        branch=args.branch,
        run_number=args.run_number,
    )
    if status.pushed:
        print(status.reason)
    else:
        print(f"::warning::{status.reason}")
    # Persistence is deliberately best-effort. Never fail the video/release because
    # state push lost a race or GitHub had a transient write problem.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Encrypted persistent cross-run memory helpers")
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
