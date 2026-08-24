from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Sequence


Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class ReleaseState:
    state: str
    tag: str
    assets_expected: int
    assets_verified: int = 0
    detail: str = ""


def _run(args: Sequence[str], *, run: Run = subprocess.run) -> subprocess.CompletedProcess:
    return run(list(args), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def _write_journal(path: Path, state: ReleaseState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 1, **asdict(state)}, indent=2), encoding="utf-8")
    tmp.replace(path)


def _unique_assets(assets: list[Path]) -> None:
    names = [item.name for item in assets]
    if len(names) != len(set(names)):
        raise RuntimeError("release assets contain duplicate basenames")
    missing = [str(item) for item in assets if not item.is_file()]
    if missing:
        raise RuntimeError("release assets missing before transaction: " + ", ".join(missing))


def _view(tag: str, repository: str, *, run: Run) -> dict:
    result = _run(
        ["gh", "release", "view", tag, "--repo", repository, "--json", "tagName,isDraft,assets"],
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
    title: str,
    notes: str,
    assets: list[Path],
    journal: Path,
    run: Run = subprocess.run,
) -> None:
    _unique_assets(assets)
    _write_journal(journal, ReleaseState("validated", tag, len(assets)))

    create = _run(
        ["gh", "release", "create", tag, "--repo", repository, "--draft", "--title", title, "--notes", notes],
        run=run,
    )
    if create.returncode != 0:
        _write_journal(journal, ReleaseState("create_failed", tag, len(assets), detail="draft release creation failed"))
        raise RuntimeError("draft GitHub Release creation failed")
    _write_journal(journal, ReleaseState("draft_created", tag, len(assets)))

    published = False
    try:
        upload = _run(
            ["gh", "release", "upload", tag, *[str(item) for item in assets], "--repo", repository],
            run=run,
        )
        if upload.returncode != 0:
            raise RuntimeError("GitHub Release asset upload failed")
        _write_journal(journal, ReleaseState("assets_uploaded", tag, len(assets)))

        payload = _view(tag, repository, run=run)
        if payload.get("isDraft") is not True:
            raise RuntimeError("Release escaped draft state before verification")
        remote_assets = payload.get("assets") or []
        if not isinstance(remote_assets, list):
            raise RuntimeError("GitHub Release assets response is malformed")
        remote = {
            str(item.get("name") or ""): int(item.get("size") or 0)
            for item in remote_assets
            if isinstance(item, dict)
        }
        expected = {item.name: item.stat().st_size for item in assets}
        if remote != expected:
            raise RuntimeError("GitHub Release remote assets do not exactly match reviewed local assets")
        _write_journal(journal, ReleaseState("assets_verified", tag, len(assets), len(remote)))

        publish = _run(["gh", "release", "edit", tag, "--repo", repository, "--draft=false"], run=run)
        if publish.returncode != 0:
            raise RuntimeError("GitHub Release publish transition failed")
        published = True

        final = _view(tag, repository, run=run)
        if final.get("isDraft") is not False:
            raise RuntimeError("GitHub Release did not reach published state")
        final_assets = final.get("assets") or []
        final_remote = {
            str(item.get("name") or ""): int(item.get("size") or 0)
            for item in final_assets
            if isinstance(item, dict)
        }
        if final_remote != expected:
            raise RuntimeError("published GitHub Release asset set drifted after publication")
        _write_journal(journal, ReleaseState("complete", tag, len(assets), len(final_remote)))
    except Exception as exc:
        if not published:
            _delete_draft_best_effort(tag, repository, run=run)
        state = "post_publish_verification_failed" if published else "rolled_back_or_draft_retained"
        _write_journal(journal, ReleaseState(state, tag, len(assets), detail=type(exc).__name__))
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--asset", type=Path, action="append", required=True)
    args = parser.parse_args()
    publish_release_transaction(
        repository=args.repository,
        tag=args.tag,
        title=args.title,
        notes=args.notes,
        assets=list(args.asset),
        journal=args.journal,
    )
    print("GitHub Release transaction PASS")


if __name__ == "__main__":
    main()
