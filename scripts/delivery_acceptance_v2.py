from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


CONTRACT_ID = "delivery.acceptance.v2"
SCHEMA_VERSION = 1
RELEASE_RECEIPT_SCHEMA_VERSION = 1
RELEASE_JOURNAL_SCHEMA_VERSION = 2
RELEASE_RECEIPT_NAME = "release-receipt.json"
DELIVERY_MANIFEST_NAME = "delivery-manifest.json"
FINAL_VIDEO_NAME = "final.mp4"
FINAL_MASTER_QC_NAME = "final-master-qc.json"
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_object(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Missing or unsafe Delivery acceptance source: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid Delivery acceptance JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Delivery acceptance source must be an object: {path.name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Delivery acceptance identity source is missing or unsafe: {path.name}")
    return {"file": path.name, "size": path.stat().st_size, "digest": _sha256(path)}


def _atomic_json(path: Path, payload: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _normalize_asset_map(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise RuntimeError("Release receipt asset map is missing or malformed")
    normalized: dict[str, dict[str, Any]] = {}
    for name, identity in value.items():
        if not isinstance(name, str) or not name or not isinstance(identity, dict):
            raise RuntimeError("Release receipt contains an invalid asset identity")
        size = identity.get("size")
        digest = str(identity.get("digest") or "").strip().lower()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimeError("Release receipt contains an invalid asset size")
        if not SHA256_DIGEST_RE.fullmatch(digest):
            raise RuntimeError("Release receipt contains an invalid asset SHA256")
        normalized[name] = {"size": size, "digest": digest}
    return normalized


def require_staged_delivery_manifest(path: Path) -> dict[str, Any]:
    manifest = _read_object(path)
    if manifest.get("schema_version") != 2:
        raise RuntimeError("Delivery manifest schema is unsupported")
    if manifest.get("release_state") != "staged":
        raise RuntimeError("Delivery manifest must remain staged until the Release transaction completes")
    if manifest.get("release_tag") not in {None, ""}:
        raise RuntimeError("Staged Delivery manifest must not claim an authoritative Release tag")
    if manifest.get("delivery_url") not in {None, ""}:
        raise RuntimeError("Staged Delivery manifest must not claim an authoritative Release URL")
    if manifest.get("publication_performed") is not False:
        raise RuntimeError("Delivery manifest must not claim YouTube publication")
    if manifest.get("youtube_publish_mode") != "manual_in_youtube_studio":
        raise RuntimeError("Delivery manifest changed the manual YouTube publication boundary")
    primary = str(manifest.get("primary_video_sha256") or "").strip().lower()
    if not SHA256_RE.fullmatch(primary):
        raise RuntimeError("Delivery manifest primary video SHA256 is missing or malformed")
    return manifest


def seal_delivery_acceptance(
    *,
    delivery_manifest: Path,
    release_receipt: Path,
    release_journal: Path,
    repository: str,
    release_tag: str,
    target_sha: str,
    output: Path,
) -> dict[str, Any]:
    delivery_manifest = Path(delivery_manifest)
    release_receipt = Path(release_receipt)
    release_journal = Path(release_journal)
    target_sha = str(target_sha or "").strip().lower()
    release_tag = str(release_tag or "").strip()
    repository = str(repository or "").strip()
    if not repository or "/" not in repository:
        raise RuntimeError("Delivery acceptance requires an exact repository identity")
    if not release_tag:
        raise RuntimeError("Delivery acceptance requires an exact Release tag")
    if not SHA1_RE.fullmatch(target_sha):
        raise RuntimeError("Delivery acceptance target SHA must be an exact 40-character commit SHA")

    manifest = require_staged_delivery_manifest(delivery_manifest)
    receipt = _read_object(release_receipt)
    journal = _read_object(release_journal)

    if receipt.get("schema_version") != RELEASE_RECEIPT_SCHEMA_VERSION:
        raise RuntimeError("Release receipt schema is unsupported")
    if receipt.get("tag") != release_tag or str(receipt.get("target_sha") or "").strip().lower() != target_sha:
        raise RuntimeError("Release receipt identity does not match the current Delivery transaction")
    assets = _normalize_asset_map(receipt.get("assets"))

    manifest_remote = assets.get(DELIVERY_MANIFEST_NAME)
    manifest_local = _identity(delivery_manifest)
    if manifest_remote != {"size": manifest_local["size"], "digest": manifest_local["digest"]}:
        raise RuntimeError("Published Release does not bind the exact staged Delivery manifest")

    final_remote = assets.get(FINAL_VIDEO_NAME)
    if not isinstance(final_remote, dict):
        raise RuntimeError("Published Release receipt is missing final.mp4")
    if final_remote.get("digest") != "sha256:" + str(manifest["primary_video_sha256"]).lower():
        raise RuntimeError("Published Release final.mp4 does not match the P4-certified Delivery video")
    if FINAL_MASTER_QC_NAME not in assets:
        raise RuntimeError("Published Release receipt is missing Final Master QC evidence")

    if journal.get("schema_version") != RELEASE_JOURNAL_SCHEMA_VERSION:
        raise RuntimeError("Release transaction journal schema is unsupported")
    if journal.get("state") != "complete":
        raise RuntimeError("Delivery cannot become released before the Release transaction is complete")
    if journal.get("tag") != release_tag or str(journal.get("target_sha") or "").strip().lower() != target_sha:
        raise RuntimeError("Release transaction journal identity does not match Delivery")
    expected_count = len(assets) + 1  # payload assets plus the published release receipt itself
    if journal.get("assets_expected") != expected_count or journal.get("assets_verified") != expected_count:
        raise RuntimeError("Release transaction journal did not verify the complete published asset set")
    journal_digests = journal.get("asset_digests")
    if not isinstance(journal_digests, dict):
        raise RuntimeError("Release transaction journal asset digests are missing")
    receipt_digest = _sha256(release_receipt)
    if str(journal_digests.get(RELEASE_RECEIPT_NAME) or "").strip().lower() != receipt_digest:
        raise RuntimeError("Release transaction journal is not bound to the exact durable Release receipt")
    for name, identity in assets.items():
        if str(journal_digests.get(name) or "").strip().lower() != identity["digest"]:
            raise RuntimeError("Release transaction journal drifted from the durable Release receipt")

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "decision": "released",
        "release_state": "released",
        "repository": repository,
        "release_tag": release_tag,
        "target_sha": target_sha,
        "delivery_url": f"https://github.com/{repository}/releases/tag/{release_tag}",
        "publication_performed": False,
        "youtube_publish_mode": "manual_in_youtube_studio",
        "delivery_manifest": manifest_local,
        "release_receipt": _identity(release_receipt),
        "release_transaction_journal": _identity(release_journal),
        "primary_video": {
            "file": FINAL_VIDEO_NAME,
            "size": final_remote["size"],
            "digest": final_remote["digest"],
        },
        "published_asset_count": len(assets) + 1,
    }
    _atomic_json(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--delivery-manifest", required=True, type=Path)
    parser.add_argument("--release-receipt", required=True, type=Path)
    parser.add_argument("--release-journal", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    seal_delivery_acceptance(
        delivery_manifest=args.delivery_manifest,
        release_receipt=args.release_receipt,
        release_journal=args.release_journal,
        repository=args.repository,
        release_tag=args.release_tag,
        target_sha=args.target_sha,
        output=args.output,
    )
    print(args.output)


if __name__ == "__main__":
    main()
