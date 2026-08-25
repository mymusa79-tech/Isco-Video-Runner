from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence


Run = Callable[..., subprocess.CompletedProcess]
METADATA_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 1800
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class ReleaseState:
    state: str
    tag: str
    assets_expected: int
    assets_verified: int = 0
    target_sha: str = ""
    asset_digests: dict[str, str] = field(default_factory=dict)
    detail: str = ""


def _run(
    args: Sequence[str],
    *,
    run: Run = subprocess.run,
    timeout: int = METADATA_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    return run(
        list(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _write_journal(path: Path, state: ReleaseState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 2, **asdict(state)}, indent=2), encoding="utf-8")
    tmp.replace(path)


def _unique_assets(assets: list[Path]) -> None:
    names = [item.name for item in assets]
    if len(names) != len(set(names)):
        raise RuntimeError("release assets contain duplicate basenames")
    missing = [str(item) for item in assets if not item.is_file()]
    if missing:
        raise RuntimeError("release assets missing before transaction: " + ", ".join(missing))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _expected_assets(assets: list[Path]) -> dict[str, dict[str, object]]:
    return {
        item.name: {"size": item.stat().st_size, "digest": _sha256(item)}
        for item in assets
    }


def _remote_assets(payload: dict, *, tag: str, target_sha: str) -> dict[str, dict[str, object]]:
    if payload.get("tagName") != tag:
        raise RuntimeError("GitHub Release tag identity does not match requested tag")
    remote_target = str(payload.get("targetCommitish") or "").strip().lower()
    if remote_target != target_sha:
        raise RuntimeError("GitHub Release target does not match reviewed Runner SHA")

    items = payload.get("assets")
    if not isinstance(items, list):
        raise RuntimeError("GitHub Release assets response is malformed")
    remote: dict[str, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("GitHub Release asset entry is malformed")
        name = str(item.get("name") or "")
        if not name or name in remote:
            raise RuntimeError("GitHub Release contains missing or duplicate asset names")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError("GitHub Release asset size is malformed")
        digest = str(item.get("digest") or "").strip().lower()
        if not SHA256_DIGEST_RE.fullmatch(digest):
            raise RuntimeError("GitHub Release asset SHA256 digest is missing or malformed")
        remote[name] = {"size": size, "digest": digest}
    return remote


def _assert_remote_matches(
    payload: dict,
    *,
    tag: str,
    target_sha: str,
    expected: dict[str, dict[str, object]],
) -> int:
    remote = _remote_assets(payload, tag=tag, target_sha=target_sha)
    if remote != expected:
        raise RuntimeError("GitHub Release remote assets do not exactly match reviewed local assets and SHA256 digests")
    return len(remote)


def _view(tag: str, repository: str, *, run: Run) -> dict:
    result = _run(
        [
            "gh", "release", "view", tag, "--repo", repository,
            "--json", "tagName,isDraft,targetCommitish,assets",
        ],
        run=run,
    )
    if result.returncode != 0:
        raise RuntimeError("could not verify GitHub Release state")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Release verification returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Release verification returned non-object JSON")
    return payload


def _delete_draft_best_effort(tag: str, repository: str, *, run: Run) -> None:
    try:
        payload = _view(tag, repository, run=run)
    except Exception:
        return
    if payload.get("isDraft") is True:
        _run(["gh", "release", "delete", tag, "--repo", repository, "--yes"], run=run)


def publish_release_transaction(
    *,
    repository: str,
    tag: str,
    target_sha: str,
    title: str,
    notes: str,
    assets: list[Path],
    journal: Path,
    run: Run = subprocess.run,
) -> None:
    target_sha = target_sha.strip().lower()
    if not SHA1_RE.fullmatch(target_sha):
        raise RuntimeError("release target_sha must be an exact 40-character commit SHA")
    _unique_assets(assets)
    expected = _expected_assets(assets)
    expected_digests = {name: str(identity["digest"]) for name, identity in expected.items()}

    def record(state: str, *, verified: int = 0, detail: str = "") -> None:
        _write_journal(
            journal,
            ReleaseState(
                state=state,
                tag=tag,
                assets_expected=len(assets),
                assets_verified=verified,
                target_sha=target_sha,
                asset_digests=expected_digests,
                detail=detail,
            ),
        )

    record("validated")

    try:
        create = _run(
            [
                "gh", "release", "create", tag, "--repo", repository,
                "--target", target_sha, "--draft", "--title", title, "--notes", notes,
            ],
            run=run,
        )
    except subprocess.TimeoutExpired as exc:
        record("create_failed", detail="draft release creation timed out")
        raise RuntimeError("draft GitHub Release creation timed out") from exc
    if create.returncode != 0:
        record("create_failed", detail="draft release creation failed")
        raise RuntimeError("draft GitHub Release creation failed")
    record("draft_created")

    published = False
    try:
        upload = _run(
            ["gh", "release", "upload", tag, *[str(item) for item in assets], "--repo", repository],
            run=run,
            timeout=UPLOAD_TIMEOUT_SECONDS,
        )
        if upload.returncode != 0:
            raise RuntimeError("GitHub Release asset upload failed")
        record("assets_uploaded")

        payload = _view(tag, repository, run=run)
        if payload.get("isDraft") is not True:
            raise RuntimeError("Release escaped draft state before verification")
        verified = _assert_remote_matches(
            payload, tag=tag, target_sha=target_sha, expected=expected
        )
        record("assets_verified", verified=verified)

        publish = _run(["gh", "release", "edit", tag, "--repo", repository, "--draft=false"], run=run)
        if publish.returncode != 0:
            raise RuntimeError("GitHub Release publish transition failed")
        published = True

        final = _view(tag, repository, run=run)
        if final.get("isDraft") is not False:
            raise RuntimeError("GitHub Release did not reach published state")
        try:
            final_verified = _assert_remote_matches(
                final, tag=tag, target_sha=target_sha, expected=expected
            )
        except RuntimeError as exc:
            raise RuntimeError("published GitHub Release identity or asset set drifted after publication") from exc
        record("complete", verified=final_verified)
    except Exception as exc:
        if not published:
            _delete_draft_best_effort(tag, repository, run=run)
        state = "post_publish_verification_failed" if published else "rolled_back_or_draft_retained"
        record(state, detail=type(exc).__name__)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--asset", type=Path, action="append", required=True)
    args = parser.parse_args()
    publish_release_transaction(
        repository=args.repository,
        tag=args.tag,
        target_sha=args.target_sha,
        title=args.title,
        notes=args.notes,
        assets=list(args.asset),
        journal=args.journal,
    )
    print("GitHub Release transaction PASS")


if __name__ == "__main__":
    main()
