from __future__ import annotations

import json
from pathlib import Path

EXPECTED_THUMBNAILS = ("thumbnail-1.jpg", "thumbnail-2.jpg", "thumbnail-3.jpg")
_ALLOWED_STOCK_PROVIDERS = {"pexels", "pixabay"}
_DERIVED_FINAL_RENDER_PROVIDER = "derived_final_render"


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
    alter release authority; it proves that three Title+Thumbnail candidates and their
    rights/Gold evidence are present and mutually consistent. Normally thumbnails are
    Pexels/Pixabay assets reviewed by Gold. Under the explicit P2 budget-safe fallback,
    they may instead be exact frame derivatives from final.mp4; that path is accepted
    only when the rights manifest proves inheritance from the already-cleared final-cut
    visuals and the thumbnail plan marks the budget degradation explicitly.
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

    derived_records = 0
    for item in thumbnail_rights:
        provider = str(item.get("provider") or "").strip().lower()
        if provider in _ALLOWED_STOCK_PROVIDERS:
            if not str(item.get("license_url") or "").strip():
                raise RuntimeError(f"Missing license URL for delivered {provider} thumbnail")
            if item.get("provider_asset_id") in (None, ""):
                raise RuntimeError(f"Missing provider asset ID for delivered {provider} thumbnail")
            continue

        if provider != _DERIVED_FINAL_RENDER_PROVIDER:
            raise RuntimeError(f"Unsupported delivered thumbnail provider: {provider or 'missing'}")
        derived_records += 1
        if package.get("budget_degraded") is not True:
            raise RuntimeError("Final-render thumbnail derivatives require explicit budget_degraded package evidence")
        if str(item.get("source_file") or "") != "final.mp4":
            raise RuntimeError("Final-render thumbnail derivative must bind to final.mp4")
        if str(item.get("rights_inheritance") or "") != "rights-manifest.visuals":
            raise RuntimeError("Final-render thumbnail derivative lacks visual-rights inheritance")
        if int(item.get("inherited_visual_rights_count") or 0) < 1:
            raise RuntimeError("Final-render thumbnail derivative has no inherited visual rights")
        if item.get("provider_asset_id") in (None, ""):
            raise RuntimeError("Final-render thumbnail derivative lacks timestamp provenance")

    if derived_records not in {0, 3}:
        raise RuntimeError("Packaging cannot mix stock thumbnails with budget-fallback final-render derivatives")
    if derived_records == 3:
        fallback = package.get("budget_fallback")
        if not isinstance(fallback, dict) or fallback.get("provider_attempts_consumed") != 0:
            raise RuntimeError("Budget-fallback packaging must prove zero thumbnail provider attempts")
        if rights.get("thumbnail_rights_mode") != "derived_from_already_rights_cleared_final_render":
            raise RuntimeError("Budget-fallback thumbnail rights mode is missing or inconsistent")

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
