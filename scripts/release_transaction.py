from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Sequence


Run = Callable[..., subprocess.CompletedProcess]
METADATA_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 1800
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RECEIPT_NAME = "release-receipt.json"
RECEIPT_SCHEMA_VERSION = 1
_MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".webm"})


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


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_journal(path: Path, state: ReleaseState) -> None:
    _atomic_json(path, {"schema_version": 2, **asdict(state)})


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


def _write_release_receipt(
    path: Path,
    *,
    tag: str,
    target_sha: str,
    payload_assets: dict[str, dict[str, object]],
) -> Path:
    if RECEIPT_NAME in payload_assets:
        raise RuntimeError("release payload must not contain the reserved receipt basename")
    _atomic_json(
        path,
        {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "tag": tag,
            "target_sha": target_sha,
            "assets": payload_assets,
        },
    )
    return path


def _assert_release_identity(payload: dict, *, tag: str, target_sha: str) -> None:
    if payload.get("tagName") != tag:
        raise RuntimeError("GitHub Release tag identity does not match requested tag")
    remote_target = str(payload.get("targetCommitish") or "").strip().lower()
    if remote_target != target_sha:
        raise RuntimeError("GitHub Release target does not match reviewed Runner SHA")


def _remote_assets(payload: dict, *, tag: str, target_sha: str) -> dict[str, dict[str, object]]:
    _assert_release_identity(payload, tag=tag, target_sha=target_sha)

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


def _view_command(tag: str, repository: str) -> list[str]:
    return [
        "gh", "release", "view", tag, "--repo", repository,
        "--json", "tagName,isDraft,targetCommitish,assets",
    ]


def _parse_view_result(result: subprocess.CompletedProcess) -> dict:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub Release verification returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("GitHub Release verification returned non-object JSON")
    return payload


def _view(tag: str, repository: str, *, run: Run) -> dict:
    result = _run(_view_command(tag, repository), run=run)
    if result.returncode != 0:
        raise RuntimeError("could not verify GitHub Release state")
    return _parse_view_result(result)


def _view_if_exists(tag: str, repository: str, *, run: Run) -> dict | None:
    """Return an existing Release or prove absence without treating probe errors as absence."""
    try:
        result = _run(_view_command(tag, repository), run=run)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub Release reconciliation probe timed out") from exc
    if result.returncode == 0:
        return _parse_view_result(result)
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    if "release not found" in detail or "http 404" in detail or "not found" in detail:
        return None
    raise RuntimeError("could not prove GitHub Release absence during reconciliation")


def _receipt_asset_map(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise RuntimeError("published Release receipt asset map is malformed")
    normalized: dict[str, dict[str, object]] = {}
    for name, identity in value.items():
        if not isinstance(name, str) or not name or name == RECEIPT_NAME or not isinstance(identity, dict):
            raise RuntimeError("published Release receipt contains invalid asset identity")
        size = identity.get("size")
        digest = str(identity.get("digest") or "").strip().lower()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or not SHA256_DIGEST_RE.fullmatch(digest):
            raise RuntimeError("published Release receipt contains malformed asset size or digest")
        normalized[name] = {"size": size, "digest": digest}
    if not normalized:
        raise RuntimeError("published Release receipt contains no payload assets")
    return normalized


def _download_and_verify_receipt(
    payload: dict,
    *,
    tag: str,
    repository: str,
    target_sha: str,
    run: Run,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    remote = _remote_assets(payload, tag=tag, target_sha=target_sha)
    receipt_identity = remote.get(RECEIPT_NAME)
    if not isinstance(receipt_identity, dict):
        raise RuntimeError("published GitHub Release is missing durable reconciliation receipt")

    with tempfile.TemporaryDirectory(prefix="isco-release-receipt-") as tmp:
        result = _run(
            [
                "gh", "release", "download", tag, "--repo", repository,
                "--pattern", RECEIPT_NAME, "--dir", tmp,
            ],
            run=run,
        )
        if result.returncode != 0:
            raise RuntimeError("could not download published Release reconciliation receipt")
        receipt_path = Path(tmp) / RECEIPT_NAME
        if not receipt_path.is_file() or receipt_path.is_symlink():
            raise RuntimeError("published Release reconciliation receipt was not downloaded safely")
        if receipt_path.stat().st_size != receipt_identity.get("size") or _sha256(receipt_path) != receipt_identity.get("digest"):
            raise RuntimeError("published Release reconciliation receipt bytes do not match GitHub digest evidence")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("published Release reconciliation receipt is malformed JSON") from exc

    if not isinstance(receipt, dict):
        raise RuntimeError("published Release reconciliation receipt must be an object")
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("published Release reconciliation receipt schema is unsupported")
    if receipt.get("tag") != tag or str(receipt.get("target_sha") or "").strip().lower() != target_sha:
        raise RuntimeError("published Release reconciliation receipt identity does not match current transaction")
    receipt_assets = _receipt_asset_map(receipt.get("assets"))
    remote_payload = {name: identity for name, identity in remote.items() if name != RECEIPT_NAME}
    if remote_payload != receipt_assets:
        raise RuntimeError("published Release assets drifted from their durable reconciliation receipt")
    return receipt_assets, remote


def _assert_current_media_matches_receipt(
    assets: list[Path],
    receipt_assets: dict[str, dict[str, object]],
) -> None:
    """Prevent downstream state from binding to a different video than the published one.

    JSON telemetry may legitimately change across GitHub run attempts (timestamps,
    attempt number, observer timing), so it is not a safe retry identity. Final/sibling
    video bytes are the semantic publication identity and must remain exact.
    """
    media = [path for path in assets if path.suffix.lower() in _MEDIA_SUFFIXES]
    if not media:
        raise RuntimeError("release reconciliation has no local video asset to bind")
    for path in media:
        expected = receipt_assets.get(path.name)
        current = {"size": path.stat().st_size, "digest": _sha256(path)}
        if expected != current:
            raise RuntimeError("current reviewed video bytes do not match the already-published Release receipt")


def _delete_draft_best_effort(tag: str, repository: str, *, run: Run) -> None:
    try:
        payload = _view(tag, repository, run=run)
    except Exception:
        return
    if payload.get("isDraft") is True:
        _run(
            ["gh", "release", "delete", tag, "--repo", repository, "--yes", "--cleanup-tag"],
            run=run,
        )


def _remove_exact_existing_draft(
    tag: str,
    repository: str,
    *,
    target_sha: str,
    run: Run,
) -> None:
    """Strictly remove only a draft proven to belong to this exact reviewed SHA."""
    payload = _view(tag, repository, run=run)
    _assert_release_identity(payload, tag=tag, target_sha=target_sha)
    if payload.get("isDraft") is not True:
        raise RuntimeError("existing GitHub Release changed state before draft reconciliation")
    result = _run(
        ["gh", "release", "delete", tag, "--repo", repository, "--yes", "--cleanup-tag"],
        run=run,
    )
    if result.returncode != 0:
        raise RuntimeError("could not remove exact stale draft Release for reconciliation")
    if _view_if_exists(tag, repository, run=run) is not None:
        raise RuntimeError("stale draft Release still exists after reconciliation cleanup")


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
    assets = [Path(item) for item in assets]
    _unique_assets(assets)
    if any(item.name == RECEIPT_NAME for item in assets):
        raise RuntimeError("release assets contain reserved durable receipt basename")

    # P6/F25: if a canonical Delivery manifest is part of the release payload, prove
    # its staged authority and exact Final Master video+QC identities before the first
    # GitHub Release probe/create/upload side effect. Terminal publication truth still
    # belongs to this transaction's durable receipt + complete journal.
    delivery_assets = [item for item in assets if item.name == "delivery-manifest.json"]
    if delivery_assets:
        from scripts.delivery_acceptance_v2 import validate_staged_delivery_assets

        validate_staged_delivery_assets(
            delivery_manifest=delivery_assets[0],
            assets=assets,
        )

    payload_expected = _expected_assets(assets)
    receipt_path = _write_release_receipt(
        Path(journal).with_name(RECEIPT_NAME),
        tag=tag,
        target_sha=target_sha,
        payload_assets=payload_expected,
    )
    transaction_assets = [*assets, receipt_path]
    expected = _expected_assets(transaction_assets)

    def record(
        state: str,
        *,
        verified: int = 0,
        detail: str = "",
        identities: dict[str, dict[str, object]] | None = None,
    ) -> None:
        evidence = expected if identities is None else identities
        _write_journal(
            journal,
            ReleaseState(
                state=state,
                tag=tag,
                assets_expected=len(evidence),
                assets_verified=verified,
                target_sha=target_sha,
                asset_digests={name: str(identity["digest"]) for name, identity in evidence.items()},
                detail=detail,
            ),
        )

    record("validated")

    try:
        existing = _view_if_exists(tag, repository, run=run)
    except Exception as exc:
        record("reconciliation_probe_failed", detail=type(exc).__name__)
        raise
    if existing is not None:
        try:
            _assert_release_identity(existing, tag=tag, target_sha=target_sha)
        except Exception as exc:
            record("existing_release_conflict", detail=type(exc).__name__)
            raise
        is_draft = existing.get("isDraft")
        if is_draft is False:
            try:
                receipt_assets, remote_identities = _download_and_verify_receipt(
                    existing,
                    tag=tag,
                    repository=repository,
                    target_sha=target_sha,
                    run=run,
                )
                _assert_current_media_matches_receipt(assets, receipt_assets)
            except Exception as exc:
                record("existing_published_conflict", detail=type(exc).__name__)
                raise
            record(
                "complete",
                verified=len(remote_identities),
                detail="reconciled_existing_published_receipt",
                identities=remote_identities,
            )
            return
        if is_draft is not True:
            record("existing_release_conflict", detail="invalid_draft_state")
            raise RuntimeError("existing GitHub Release has invalid draft state")
        record("existing_draft_detected")
        try:
            _remove_exact_existing_draft(
                tag,
                repository,
                target_sha=target_sha,
                run=run,
            )
        except Exception as exc:
            record("existing_draft_cleanup_failed", detail=type(exc).__name__)
            raise
        record("existing_draft_removed")

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
            ["gh", "release", "upload", tag, *[str(item) for item in transaction_assets], "--repo", repository],
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