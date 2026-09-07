from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from isco_video_agent.media.ffmpeg import duration


class SourceDerivedVisualInheritanceError(RuntimeError):
    pass


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, expected: type) -> Any:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceDerivedVisualInheritanceError(
            f"source_visual_inheritance_invalid_json:{Path(path).name}"
        ) from exc
    if not isinstance(payload, expected):
        raise SourceDerivedVisualInheritanceError(
            f"source_visual_inheritance_wrong_shape:{Path(path).name}"
        )
    return payload


def resolve_parent_output(
    control_request: dict[str, Any],
    *,
    output_root: Path = Path("output"),
) -> Path:
    """Resolve the exact accepted Long output by the immutable plan hash already in the child request."""
    if _clean(control_request.get("approval_scope")) != "short_sibling":
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_requires_sibling_scope")
    expected_sha = _clean(control_request.get("source_production_plan_sha256"))
    if len(expected_sha) != 64:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_parent_plan_hash_missing")

    matches: list[Path] = []
    root = Path(output_root)
    for plan_path in root.glob("*/plan.json"):
        if not plan_path.is_file():
            continue
        try:
            if _sha256_file(plan_path) == expected_sha:
                matches.append(plan_path.parent.resolve())
        except OSError:
            continue
    if len(matches) != 1:
        raise SourceDerivedVisualInheritanceError(
            "source_visual_inheritance_parent_output_not_unique:"
            f"matches={len(matches)}"
        )
    parent = matches[0]
    final_path = parent / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_parent_final_missing")
    return parent


def _source_section_binding(
    parent_plan: dict[str, Any],
    control_request: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    sections = parent_plan.get("sections")
    if not isinstance(sections, list):
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_parent_sections_missing")
    excerpt = control_request.get("source_episode_excerpt")
    if not isinstance(excerpt, dict):
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_excerpt_missing")
    expected_id = _clean(excerpt.get("source_section_id"))
    expected_job = _clean(control_request.get("source_semantic_job"))
    expected_query = _clean(excerpt.get("source_visual_query"))
    matches: list[tuple[int, dict[str, Any]]] = []
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            continue
        if _clean(section.get("id")) != expected_id:
            continue
        if _clean(section.get("key_point")) != expected_job:
            continue
        if _clean(section.get("visual_query")) != expected_query:
            continue
        matches.append((index, section))
    if len(matches) != 1:
        raise SourceDerivedVisualInheritanceError(
            "source_visual_inheritance_section_binding_not_unique:"
            f"matches={len(matches)}"
        )
    return matches[0]


def _section_prepared_clips(parent_output: Path, section_index: int) -> list[Path]:
    clips_root = parent_output / "clips"
    prefix = f"{section_index:02d}"
    opening = sorted(clips_root.glob(f"{prefix}-opening-*-prepared.mp4"))
    sequence = sorted(clips_root.glob(f"{prefix}-sequence-*-prepared.mp4"))
    single = clips_root / f"{prefix}-prepared.mp4"
    if opening:
        clips = opening
    elif sequence:
        clips = sequence
    elif single.is_file():
        clips = [single]
    else:
        clips = []
    usable = [path for path in clips if path.is_file() and path.stat().st_size > 1024]
    if not usable:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_section_clips_missing")
    return usable


def _all_prepared_clips(parent_output: Path) -> list[Path]:
    clips = sorted(
        path
        for path in (parent_output / "clips").glob("*-prepared.mp4")
        if path.is_file() and path.stat().st_size > 1024
    )
    if not clips:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_parent_clips_missing")
    return clips


def _provider_asset_key(record: dict[str, Any]) -> tuple[str, str]:
    provider = _clean(record.get("provider")).lower()
    asset_id = record.get("asset_id")
    if asset_id in (None, ""):
        asset_id = record.get("pexels_id") if provider == "pexels" else record.get("pixabay_id")
    return provider, _clean(asset_id)


def _audit_for_asset(
    visual_audits: list[dict[str, Any]],
    *,
    section_id: str,
    provider: str,
    asset_id: str,
) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for raw in visual_audits:
        if not isinstance(raw, dict):
            continue
        if _clean(raw.get("section")) != section_id:
            continue
        if _clean(raw.get("provider")).lower() != provider:
            continue
        if _clean(raw.get("candidate_id")) != asset_id:
            continue
        if raw.get("status") != "pass":
            continue
        candidates.append(raw)
    if not candidates:
        raise SourceDerivedVisualInheritanceError(
            "source_visual_inheritance_parent_audit_missing:"
            f"provider={provider}:asset={asset_id}"
        )
    selected = next((item for item in candidates if item.get("is_selected") is True), candidates[0])
    return dict(selected)


def resolve_inherited_visuals(
    control_request: dict[str, Any],
    *,
    output_root: Path = Path("output"),
) -> dict[str, Any]:
    """Return exact parent section clips plus their already-certified audit/rights provenance.

    This deliberately reuses bytes that already reached the accepted Long render. No stock
    search, Vision review or text model is needed to establish the sibling's visual source.
    """
    parent = resolve_parent_output(control_request, output_root=output_root)
    parent_plan_path = parent / "plan.json"
    parent_plan = _read_json(parent_plan_path, dict)
    section_index, section = _source_section_binding(parent_plan, control_request)
    section_id = _clean(section.get("id"))
    section_clips = _section_prepared_clips(parent, section_index)

    credits_raw = _read_json(parent / "credits.json", list)
    credits = [item for item in credits_raw if isinstance(item, dict)]
    all_clips = _all_prepared_clips(parent)
    if len(credits) != len(all_clips):
        raise SourceDerivedVisualInheritanceError(
            "source_visual_inheritance_credit_clip_cardinality_mismatch:"
            f"credits={len(credits)}:clips={len(all_clips)}"
        )
    credit_by_clip = {clip.resolve(): dict(credit) for clip, credit in zip(all_clips, credits)}

    rights = _read_json(parent / "rights-manifest.json", dict)
    rights_visuals = [item for item in list(rights.get("visuals") or []) if isinstance(item, dict)]
    rights_by_key = {_provider_asset_key(item): dict(item) for item in rights_visuals}
    visual_audits = [
        item for item in _read_json(parent / "visual-audit.json", list) if isinstance(item, dict)
    ]

    assets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for clip in section_clips:
        credit = credit_by_clip.get(clip.resolve())
        if not isinstance(credit, dict):
            raise SourceDerivedVisualInheritanceError(
                f"source_visual_inheritance_credit_missing:{clip.name}"
            )
        provider, asset_id = _provider_asset_key(credit)
        if not provider or not asset_id:
            raise SourceDerivedVisualInheritanceError(
                f"source_visual_inheritance_asset_identity_missing:{clip.name}"
            )
        key = (provider, asset_id)
        if key in seen:
            raise SourceDerivedVisualInheritanceError("source_visual_inheritance_duplicate_parent_asset")
        seen.add(key)
        rights_entry = rights_by_key.get(key)
        if not isinstance(rights_entry, dict):
            raise SourceDerivedVisualInheritanceError(
                f"source_visual_inheritance_rights_missing:{provider}:{asset_id}"
            )
        audit = _audit_for_asset(
            visual_audits,
            section_id=section_id,
            provider=provider,
            asset_id=asset_id,
        )
        assets.append(
            {
                "path": clip,
                "filename": clip.name,
                "sha256": _sha256_file(clip),
                "duration_seconds": round(float(duration(clip)), 3),
                "provider": provider,
                "asset_id": asset_id,
                "credit": credit,
                "rights": rights_entry,
                "parent_audit": audit,
            }
        )

    total_seconds = sum(float(item["duration_seconds"]) for item in assets)
    if total_seconds <= 0:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_duration_missing")
    return {
        "schema_version": 1,
        "mode": "exact_parent_section_prepared_assets",
        "source_production_plan_sha256": _sha256_file(parent_plan_path),
        "source_parent_final_sha256": _sha256_file(parent / "final.mp4"),
        "source_section_id": section_id,
        "source_section_index": section_index,
        "source_visual_query": _clean(section.get("visual_query")),
        "source_parent_output": str(parent),
        "asset_count": len(assets),
        "total_visual_seconds": round(total_seconds, 3),
        "assets": assets,
        "extra_stock_queries": 0,
        "extra_vision_ai_calls": 0,
        "extra_text_ai_calls": 0,
    }


def inherited_visual_seconds(inheritance: dict[str, Any]) -> float:
    try:
        value = float(inheritance.get("total_visual_seconds") or 0.0)
    except (TypeError, ValueError) as exc:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_duration_invalid") from exc
    if value <= 0:
        raise SourceDerivedVisualInheritanceError("source_visual_inheritance_duration_invalid")
    return value
