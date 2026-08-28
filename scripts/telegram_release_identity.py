from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Sequence


Run = Callable[..., subprocess.CompletedProcess]
SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_COMMON_REQUIRED_ASSETS = frozenset(
    {
        "final.mp4",
        "delivery-manifest.json",
        "plan.json",
        "quality-final.json",
        "final-master-qc.json",
        "final-critic.json",
        "ai-budget.json",
        "production-manifest.json",
        "rights-manifest.json",
        "gold-enforce-report.json",
    }
)


def _run(args: Sequence[str], *, run: Run = subprocess.run) -> subprocess.CompletedProcess:
    return run(
        list(args),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=120,
    )


def _required_assets(request: dict) -> set[str]:
    required = set(_COMMON_REQUIRED_ASSETS)
    if request.get("approval_scope") == "long_plus_sibling_shorts":
        required.update({"sibling-short-plan.json", "sibling-short-results.json"})
    return required


def verify_existing_release(
    *,
    repository: str,
    tag: str,
    target_sha: str,
    request: dict,
    run: Run = subprocess.run,
) -> dict:
    target_sha = str(target_sha or "").strip().lower()
    if not SHA1_RE.fullmatch(target_sha):
        raise RuntimeError("Telegram release verification requires an exact 40-character Runner SHA")

    result = _run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,isDraft,targetCommitish,assets",
        ],
        run=run,
    )
    if result.returncode != 0:
        raise RuntimeError("Telegram release exists check could not resolve the expected release")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram release verification returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Telegram release verification returned a non-object payload")

    if payload.get("tagName") != tag:
        raise RuntimeError("Telegram release tag identity mismatch")
    if payload.get("isDraft") is not False:
        raise RuntimeError("Telegram release is not a completed published release")
    if str(payload.get("targetCommitish") or "").strip().lower() != target_sha:
        raise RuntimeError("Telegram release target does not match the reviewed Runner SHA")

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("Telegram release assets payload is malformed")
    resolved: dict[str, dict] = {}
    for item in assets:
        if not isinstance(item, dict):
            raise RuntimeError("Telegram release contains a malformed asset entry")
        name = str(item.get("name") or "").strip()
        if not name or name in resolved:
            raise RuntimeError("Telegram release contains a missing or duplicate asset name")
        size = item.get("size")
        digest = str(item.get("digest") or "").strip().lower()
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise RuntimeError(f"Telegram release asset has invalid size: {name}")
        if not SHA256_RE.fullmatch(digest):
            raise RuntimeError(f"Telegram release asset lacks a valid SHA256 digest: {name}")
        resolved[name] = {"size": size, "digest": digest}

    missing = sorted(_required_assets(request) - set(resolved))
    if missing:
        raise RuntimeError("Telegram release is incomplete; missing required assets: " + ", ".join(missing))

    if request.get("approval_scope") == "long_plus_sibling_shorts":
        short_assets = [name for name in resolved if name.startswith("short-")]
        if len(short_assets) < 2:
            raise RuntimeError("Telegram long+Shorts release is missing the approved sibling Short assets")

    return {
        "tag": tag,
        "target_sha": target_sha,
        "asset_count": len(resolved),
        "required_assets": sorted(_required_assets(request)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise SystemExit("Telegram release request must be a JSON object")
    evidence = verify_existing_release(
        repository=args.repository,
        tag=args.tag,
        target_sha=args.target_sha,
        request=request,
    )
    print(
        "Telegram release identity PASS: "
        f"tag={evidence['tag']} target={evidence['target_sha']} assets={evidence['asset_count']}"
    )


if __name__ == "__main__":
    main()
