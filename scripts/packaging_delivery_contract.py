from __future__ import annotations

import json
from pathlib import Path

EXPECTED_THUMBNAILS = ("thumbnail-1.jpg", "thumbnail-2.jpg", "thumbnail-3.jpg")
_ALLOWED_PROVIDERS = {"pexels", "pixabay"}


def _read_object(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError(f"Missing packaging delivery artifact: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON packaging delivery artifact: {path.name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Packaging delivery artifact must be a JSON object: {path.name}")
    return data


def validate_packaging_delivery(root: Path) -> dict[str, Path]:
    """Validate the exact Gold packaging set before upload/release.

    This is deliberately local/deterministic. It does not call providers and does not
    alter release authority; it only proves that the three reviewed Title+Thumbnail
    candidates and their rights/Gold evidence are all present and mutually consistent.
    """
    root = Path(root)
    if not root.is_dir():
        raise RuntimeError(f"Packaging delivery root is not a directory: {root}")

    thumbnail_plan = root / "thumbnail-plan.json"
    rights_manifest = root / "rights-manifest.json"
    gold_report = root / "gold-enforce-report.json"
    thumbnails = [root / name for name in EXPECTED_THUMBNAILS]

    package = _read_object(thumbnail_plan)
    if package.get("status") != "ready":
        raise RuntimeError("Thumbnail package is not ready for delivery")
    candidates = package.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 3 or not all(isinstance(x, dict) for x in candidates):
        raise RuntimeError("Thumbnail package must contain exactly three candidate objects")

    package_files = [str(item.get("file") or "") for item in candidates]
    if package_files != list(EXPECTED_THUMBNAILS):
        raise RuntimeError("Thumbnail package candidate files do not match the A/B/C delivery contract")
    candidate_ids = [str(item.get("candidate_id") or "") for item in candidates]
    if len(set(candidate_ids)) != 3 or any(not value for value in candidate_ids):
        raise RuntimeError("Thumbnail package candidate IDs must be three non-empty unique values")

    for path in thumbnails:
        if not path.is_file():
            raise RuntimeError(f"Missing reviewed thumbnail: {path.name}")
        if path.stat().st_size <= 1024:
            raise RuntimeError(f"Reviewed thumbnail is unexpectedly small: {path.name}")

    rights = _read_object(rights_manifest)
    thumbnail_rights = rights.get("thumbnails")
    if (
        not isinstance(thumbnail_rights, list)
        or len(thumbnail_rights) != 3
        or not all(isinstance(x, dict) for x in thumbnail_rights)
    ):
        raise RuntimeError("Rights manifest must contain exactly three thumbnail rights records")
    rights_files = [str(item.get("output_file") or "") for item in thumbnail_rights]
    if rights_files != list(EXPECTED_THUMBNAILS):
        raise RuntimeError("Thumbnail rights records do not bind to the delivered A/B/C files")
    for item in thumbnail_rights:
        provider = str(item.get("provider") or "").strip().lower()
        if provider not in _ALLOWED_PROVIDERS:
            raise RuntimeError(f"Unsupported delivered thumbnail provider: {provider or 'missing'}")
        if not str(item.get("license_url") or "").strip():
            raise RuntimeError(f"Missing license URL for delivered {provider} thumbnail")
        if item.get("provider_asset_id") in (None, ""):
            raise RuntimeError(f"Missing provider asset ID for delivered {provider} thumbnail")

    gold = _read_object(gold_report)
    gold_decision = gold.get("gold")
    same_render = gold.get("same_render")
    if gold.get("phase") != "4" or gold.get("mode") != "enforce" or gold.get("release_authority") != "gold":
        raise RuntimeError("Packaging delivery requires the enforced Gold Phase 4 report")
    if gold.get("single_render") is not True:
        raise RuntimeError("Packaging delivery requires the Gold single-render invariant")
    if not isinstance(gold_decision, dict) or gold_decision.get("accepted") is not True:
        raise RuntimeError("Packaging delivery requires Gold acceptance")
    if not isinstance(same_render, dict) or same_render.get("artifact_divergence") is not False:
        raise RuntimeError("Packaging delivery blocked because final render diverged during Gold enforcement")

    return {
        "thumbnail_plan": thumbnail_plan,
        "rights_manifest": rights_manifest,
        "gold_report": gold_report,
        "thumbnail_1": thumbnails[0],
        "thumbnail_2": thumbnails[1],
        "thumbnail_3": thumbnails[2],
    }
