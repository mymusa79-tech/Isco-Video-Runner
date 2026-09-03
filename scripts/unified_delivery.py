from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.final_master_acceptance_v2 import (
    require_certified_final_video,
    require_final_master_acceptance,
)

SCHEMA_VERSION = 2


def _read_object(path: Path, *, required: bool = True) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise RuntimeError(f"Missing delivery source: {path.name}")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid delivery source: {path.name}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Delivery source must be an object: {path.name}")
    return data


def _file_identity(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Missing or unsafe delivery evidence: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"file": path.name, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _request_summary(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    return {
        "request_id": request.get("request_id"),
        "request_sha256": request.get("request_sha256"),
        "source": request.get("source"),
        "approval_scope": request.get("approval_scope"),
        "approved_topic": request.get("approved_topic"),
        "approved_at": request.get("approved_at"),
        "parent_approved_brief_sha256": request.get("parent_approved_brief_sha256"),
    }


def _validate_short_assets(root: Path, short_assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if short_assets and not 2 <= len(short_assets) <= 3:
        raise RuntimeError("Unified long-form delivery must contain 2–3 sibling Shorts")
    normalized: list[dict[str, Any]] = []
    seen_jobs: set[str] = set()
    for item in short_assets:
        if not isinstance(item, dict) or item.get("delivery_allowed") is not True:
            raise RuntimeError("Unified delivery contains an unapproved sibling Short")
        semantic_job = " ".join(str(item.get("semantic_job") or "").strip().split())
        key = semantic_job.casefold()
        if not semantic_job or key in seen_jobs:
            raise RuntimeError("Unified delivery sibling Shorts must have distinct semantic jobs")
        seen_jobs.add(key)
        video = str(item.get("video") or "")
        video_path = root / video
        if not video or not video_path.is_file():
            raise RuntimeError("Unified delivery sibling Short video is missing")
        qc_name = str(item.get("final_master_qc") or "")
        qc_path = root / qc_name
        if not qc_name or not qc_path.is_file():
            raise RuntimeError("Unified delivery sibling Short Final Master QC is missing")
        try:
            qc = require_certified_final_video(qc_path, video_path)
        except Exception as exc:
            raise RuntimeError("Unified delivery sibling Short failed exact Final Master acceptance") from exc
        normalized_item = dict(item)
        qc_identity = _file_identity(qc_path)
        normalized_item["final_master_qc"] = {
            "file": qc_name,
            "file_size": qc_identity["size"],
            "file_sha256": qc_identity["sha256"],
            "evidence": qc,
        }
        normalized.append(normalized_item)
    return normalized


def _cinematic_reports(root: Path) -> dict[str, Any]:
    known = {
        "m7_visual_timeline": "visual-timeline.json",
        "audio_mastering": "audio-mastering.json",
        "sfx": "sfx-plan.json",
        "m9_transitions": "m9-transitions.json",
        "m10_cards": "m10-cards.json",
        "m11_archive": "m11-report.json",
        "contextual_cta": "cta-plan.json",
    }
    reports: dict[str, Any] = {key: name for key, name in known.items() if (root / name).is_file()}
    dynamics_path = root / "narrative-music-dynamics.json"
    if dynamics_path.is_file():
        reports["narrative_music_dynamics"] = {
            "file": dynamics_path.name,
            "evidence": _read_object(dynamics_path),
        }
    m8 = sorted(path.name for path in root.glob("*.m8.json") if path.is_file())
    if m8:
        reports["m8_color_normalization"] = m8
    return reports


def build_delivery_manifest(
    root: Path,
    *,
    repository: str,
    release_tag: str | None,
    request: dict[str, Any] | None = None,
    short_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    plan = _read_object(root / "plan.json")
    quality = _read_object(root / "quality-final.json")
    production = _read_object(root / "production-manifest.json")
    packaging = _read_object(root / "thumbnail-plan.json", required=False)
    final = root / "final.mp4"
    if not final.is_file():
        raise RuntimeError("Missing final.mp4 for delivery")
    try:
        final_master_qc = require_final_master_acceptance(root)
    except Exception as exc:
        raise RuntimeError("Unified delivery requires exact current Final Master acceptance") from exc
    final_master_qc_path = root / "final-master-qc.json"
    final_master_qc_identity = _file_identity(final_master_qc_path)

    fmt = str(plan.get("format") or quality.get("format") or production.get("format") or "")
    kind = "short" if fmt == "moment" or str(release_tag or "").startswith("short-") else "long"

    title_thumbnail_pairs: list[dict[str, Any]] = []
    raw_candidates = packaging.get("candidates") if isinstance(packaging, dict) else None
    if isinstance(raw_candidates, list):
        for index, item in enumerate(raw_candidates[:3]):
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file") or f"thumbnail-{index + 1}.jpg")
            if not (root / file_name).is_file():
                raise RuntimeError(f"Missing thumbnail asset for delivery: {file_name}")
            title_thumbnail_pairs.append(
                {
                    "slot": str(item.get("experiment_slot") or chr(ord("A") + index)),
                    "title": str(item.get("title_ar") or ""),
                    "thumbnail": file_name,
                    "thumbnail_text": str(item.get("text_ar") or ""),
                    "hypothesis": str(item.get("packaging_hypothesis") or ""),
                }
            )

    shorts = _validate_short_assets(root, list(short_assets or []))
    candidate_tag = str(release_tag or "").strip() or None
    candidate_url = f"https://github.com/{repository}/releases/tag/{candidate_tag}" if candidate_tag else None
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "delivery_kind": "long_plus_shorts" if kind == "long" and shorts else kind,
        "topic": str(plan.get("topic") or ""),
        # Immutable reviewed staging evidence. A tag is only a candidate namespace
        # until release_transaction proves the published remote bytes.
        "release_state": "staged",
        "release_tag": None,
        "delivery_url": None,
        "release_candidate_tag": candidate_tag,
        "release_candidate_url": candidate_url,
        "primary_video": "final.mp4",
        "primary_video_sha256": final_master_qc["acceptance_contract"]["sources"]["final"]["sha256"],
        "title_thumbnail_pairs": title_thumbnail_pairs,
        "shorts": shorts,
        "short_count": len(shorts),
        "control_request": _request_summary(request),
        "youtube_publish_mode": "manual_in_youtube_studio",
        "telegram_surface": "decision_and_delivery_only",
        "publication_performed": False,
        "partial_delivery_allowed": False,
        "production_manifest": "production-manifest.json",
        "rights_manifest": "rights-manifest.json" if (root / "rights-manifest.json").is_file() else None,
        "gold_report": "gold-enforce-report.json" if (root / "gold-enforce-report.json").is_file() else None,
        "final_master_qc": {
            "file": "final-master-qc.json",
            "file_size": final_master_qc_identity["size"],
            "file_sha256": final_master_qc_identity["sha256"],
            "evidence": final_master_qc,
        },
        "canonical_bundle_request": "canonical-bundle-request.json" if (root / "canonical-bundle-request.json").is_file() else None,
        "sibling_short_plan": "sibling-short-plan.json" if (root / "sibling-short-plan.json").is_file() else None,
        "sibling_short_results": "sibling-short-results.json" if (root / "sibling-short-results.json").is_file() else None,
        "cinematic_reports": _cinematic_reports(root),
    }
    if kind == "long" and len(title_thumbnail_pairs) != 3:
        raise RuntimeError("Long-form unified delivery must expose exactly three A/B/C packaging pairs")
    if request and request.get("approval_scope") == "long_plus_sibling_shorts" and len(shorts) not in {2, 3}:
        raise RuntimeError("Approved long+Shorts request cannot stage a partial unified delivery")
    return manifest


def write_delivery_manifest(
    root: Path,
    *,
    repository: str,
    release_tag: str | None,
    request_path: Path | None = None,
    request: dict[str, Any] | None = None,
    short_assets: list[dict[str, Any]] | None = None,
    output: Path | None = None,
) -> Path:
    if request is not None and request_path is not None:
        raise RuntimeError("Provide either request or request_path, not both")
    resolved_request = request if request is not None else (_read_object(request_path, required=False) if request_path else None)
    manifest = build_delivery_manifest(
        root,
        repository=repository,
        release_tag=release_tag,
        request=resolved_request,
        short_assets=short_assets,
    )
    dest = output or (Path(root) / "delivery-manifest.json")
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def finalize_release_manifest(path: Path, *, repository: str, release_tag: str) -> Path:
    """Legacy call seam: bind only the requested Release candidate, never Released truth.

    The V4 workflow historically calls this before ``release_transaction.py``. Keeping
    that call compatible is useful, but it must not be able to make reviewed local
    evidence outrun the GitHub Release boundary. Terminal ``released`` truth belongs to
    the completed Release transaction / durable receipt and delivery.acceptance.v2.
    """
    manifest = _read_object(path)
    if manifest.get("release_state") != "staged":
        raise RuntimeError("Delivery manifest must remain staged before the Release transaction")
    tag = str(release_tag or "").strip()
    repo = str(repository or "").strip()
    if not tag or not repo:
        raise RuntimeError("Release candidate identity is incomplete")
    manifest["release_state"] = "staged"
    manifest["release_tag"] = None
    manifest["delivery_url"] = None
    manifest["release_candidate_tag"] = tag
    manifest["release_candidate_url"] = f"https://github.com/{repo}/releases/tag/{tag}"
    manifest["publication_performed"] = False
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return Path(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = write_delivery_manifest(
        args.root,
        repository=args.repository,
        release_tag=args.release_tag,
        request_path=args.request,
        output=args.output,
    )
    print(path)


if __name__ == "__main__":
    main()
