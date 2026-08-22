from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


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


def _request_summary(request: dict[str, Any] | None) -> dict[str, Any] | None:
    if not request:
        return None
    return {
        "request_id": request.get("request_id"),
        "request_sha256": request.get("request_sha256"),
        "approval_scope": request.get("approval_scope"),
        "approved_topic": request.get("approved_topic"),
        "approved_at": request.get("approved_at"),
    }


def build_delivery_manifest(
    root: Path,
    *,
    repository: str,
    release_tag: str,
    request: dict[str, Any] | None = None,
    short_assets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    root = Path(root)
    plan = _read_object(root / "plan.json")
    quality = _read_object(root / "quality-final.json")
    production = _read_object(root / "production-manifest.json")
    packaging = _read_object(root / "thumbnail-plan.json", required=False)

    fmt = str(plan.get("format") or quality.get("format") or production.get("format") or "")
    kind = "short" if fmt == "moment" or release_tag.startswith("short-") else "long"
    final = root / "final.mp4"
    if not final.is_file():
        raise RuntimeError("Missing final.mp4 for delivery")

    title_thumbnail_pairs: list[dict[str, Any]] = []
    raw_candidates = packaging.get("candidates") if isinstance(packaging, dict) else None
    if isinstance(raw_candidates, list):
        for index, item in enumerate(raw_candidates[:3]):
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file") or f"thumbnail-{index + 1}.jpg")
            title_thumbnail_pairs.append(
                {
                    "slot": str(item.get("experiment_slot") or chr(ord("A") + index)),
                    "title": str(item.get("title_ar") or ""),
                    "thumbnail": file_name,
                    "thumbnail_text": str(item.get("text_ar") or ""),
                    "hypothesis": str(item.get("packaging_hypothesis") or ""),
                }
            )

    release_url = f"https://github.com/{repository}/releases/tag/{release_tag}"
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "delivery_kind": kind,
        "topic": str(plan.get("topic") or ""),
        "release_tag": release_tag,
        "delivery_url": release_url,
        "primary_video": "final.mp4",
        "title_thumbnail_pairs": title_thumbnail_pairs,
        "shorts": list(short_assets or []),
        "control_request": _request_summary(request),
        "youtube_publish_mode": "manual_in_youtube_studio",
        "telegram_surface": "decision_and_delivery_only",
        "publication_performed": False,
        "production_manifest": "production-manifest.json",
        "rights_manifest": "rights-manifest.json" if (root / "rights-manifest.json").is_file() else None,
        "gold_report": "gold-enforce-report.json" if (root / "gold-enforce-report.json").is_file() else None,
    }
    if kind == "long" and title_thumbnail_pairs and len(title_thumbnail_pairs) != 3:
        raise RuntimeError("Long-form unified delivery must expose exactly three A/B/C packaging pairs")
    return manifest


def write_delivery_manifest(
    root: Path,
    *,
    repository: str,
    release_tag: str,
    request_path: Path | None = None,
    output: Path | None = None,
) -> Path:
    request = _read_object(request_path, required=False) if request_path else None
    manifest = build_delivery_manifest(root, repository=repository, release_tag=release_tag, request=request)
    dest = output or (Path(root) / "delivery-manifest.json")
    dest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--release-tag", required=True)
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
