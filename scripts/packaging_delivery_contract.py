from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import isco_video_agent.media.ffmpeg as media_ffmpeg

EXPECTED_THUMBNAILS = ("thumbnail-1.jpg", "thumbnail-2.jpg", "thumbnail-3.jpg")
_ALLOWED_STOCK_PROVIDERS = {"pexels", "pixabay"}
_DERIVED_FINAL_RENDER_PROVIDER = "derived_final_render"
_SHORT_FRAME_FRACTIONS = (0.18, 0.50, 0.82)
CONTRACT_ID = "gold.packaging.acceptance.v2"
CONTRACT_SCHEMA_VERSION = 2
ACCEPTANCE_FILENAME = "gold-packaging-acceptance.json"


class GoldPackagingAcceptanceError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise GoldPackagingAcceptanceError(f"Missing packaging delivery artifact: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GoldPackagingAcceptanceError(f"Invalid JSON packaging delivery artifact: {path.name}") from exc
    if not isinstance(data, dict):
        raise GoldPackagingAcceptanceError(f"Packaging delivery artifact must be a JSON object: {path.name}")
    return data


def _regular_file_binding(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise GoldPackagingAcceptanceError(f"gold_packaging_invalid_file:{path.name}")
        size = int(path.stat().st_size)
    except OSError as exc:
        raise GoldPackagingAcceptanceError(f"gold_packaging_unreadable_file:{path.name}") from exc
    if size <= 0:
        raise GoldPackagingAcceptanceError(f"gold_packaging_empty_file:{path.name}")
    return {"file": path.name, "sha256": _sha256_file(path), "byte_length": size}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def packaging_policy_fingerprint() -> str:
    policy = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "long_profile": {
            "status": "ready",
            "package_type": "title_thumbnail_hypothesis_set",
            "candidate_files": list(EXPECTED_THUMBNAILS),
            "candidate_count": 3,
            "experiment_slots": ["A", "B", "C"],
            "stock_providers": sorted(_ALLOWED_STOCK_PROVIDERS),
            "budget_fallback_provider": _DERIVED_FINAL_RENDER_PROVIDER,
            "budget_fallback_requires_rights_inheritance": True,
        },
        "short_profile": {
            "status": "not_applicable_to_shorts",
            "custom_thumbnail_candidates": 0,
            "selection_aid_files": list(EXPECTED_THUMBNAILS),
            "selection_aid_source": "exact_final_render",
            "selection_aid_ai_calls": 0,
            "selection_policy": "truthful_frame_selected_manually_during_upload",
        },
        "exact_artifact_hashes_required": True,
        "gold_report_must_bind_certificate_sha256": True,
        "youtube_publication": "manual",
        "ai_calls_added": 0,
    }
    raw = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _profile_from_package(package: dict[str, Any]) -> str:
    status = str(package.get("status") or "")
    if status == "ready":
        return "long_title_thumbnail_hypothesis_set"
    if status == "not_applicable_to_shorts":
        return "short_truthful_frame_selection"
    raise GoldPackagingAcceptanceError("Thumbnail package is not in a recognized Gold delivery state")


def _extract_frame(final_path: Path, output: Path, timestamp: float) -> None:
    media_ffmpeg._run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, timestamp):.3f}",
            "-i",
            str(final_path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
            "-q:v",
            "3",
            str(output),
        ]
    )
    if not output.is_file() or output.stat().st_size <= 1024:
        raise GoldPackagingAcceptanceError("Short frame-selection aid could not be extracted from final.mp4")


def _ensure_short_selection_frames(root: Path) -> list[dict[str, Any]]:
    final_path = root / "final.mp4"
    try:
        total_duration = float(media_ffmpeg.duration(final_path))
    except Exception as exc:
        raise GoldPackagingAcceptanceError("Short frame-selection duration probe failed") from exc
    if total_duration <= 0:
        raise GoldPackagingAcceptanceError("Short frame-selection requires positive final duration")

    evidence: list[dict[str, Any]] = []
    for filename, fraction in zip(EXPECTED_THUMBNAILS, _SHORT_FRAME_FRACTIONS):
        path = root / filename
        timestamp = min(max(0.10, total_duration * fraction), max(0.10, total_duration - 0.10))
        _extract_frame(final_path, path, timestamp)
        evidence.append(
            {
                "file": path.name,
                "source_file": "final.mp4",
                "source_timestamp_seconds": round(timestamp, 3),
                "role": "manual_short_frame_selection_aid",
            }
        )
    return evidence


def _validate_base_package(root: Path, package: dict[str, Any], rights: dict[str, Any], profile: str) -> None:
    if profile == "short_truthful_frame_selection":
        candidates = package.get("candidates")
        if candidates != []:
            raise GoldPackagingAcceptanceError("Short packaging must contain zero custom thumbnail candidates")
        if not str(package.get("reason") or "").strip():
            raise GoldPackagingAcceptanceError("Short packaging must explain why custom thumbnails are not applicable")
        thumbnail_rights = rights.get("thumbnails")
        if thumbnail_rights not in (None, []):
            raise GoldPackagingAcceptanceError("Short packaging must not claim custom thumbnail rights records")
        for path in (root / name for name in EXPECTED_THUMBNAILS):
            if not path.is_file() or path.is_symlink() or path.stat().st_size <= 1024:
                raise GoldPackagingAcceptanceError("Short packaging is missing a sealed final-render frame-selection aid")
        return

    if package.get("status") != "ready":
        raise GoldPackagingAcceptanceError("Thumbnail package is not ready for delivery")
    if package.get("package_type") not in (None, "title_thumbnail_hypothesis_set"):
        raise GoldPackagingAcceptanceError("Thumbnail package type is not the Gold hypothesis-set contract")
    candidates = package.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 3 or not all(isinstance(x, dict) for x in candidates):
        raise GoldPackagingAcceptanceError("Thumbnail package must contain exactly three candidate objects")

    package_files = [str(item.get("file") or "") for item in candidates]
    if package_files != list(EXPECTED_THUMBNAILS):
        raise GoldPackagingAcceptanceError("Thumbnail package candidate files do not match the A/B/C delivery contract")
    candidate_ids = [str(item.get("candidate_id") or "") for item in candidates]
    if len(set(candidate_ids)) != 3 or any(not value for value in candidate_ids):
        raise GoldPackagingAcceptanceError("Thumbnail package candidate IDs must be three non-empty unique values")
    slots = [str(item.get("experiment_slot") or "") for item in candidates]
    if any(slots) and slots != ["A", "B", "C"]:
        raise GoldPackagingAcceptanceError("Thumbnail package experiment slots must preserve A/B/C order")

    for path in (root / name for name in EXPECTED_THUMBNAILS):
        if not path.is_file() or path.is_symlink():
            raise GoldPackagingAcceptanceError(f"Missing reviewed thumbnail: {path.name}")
        if path.stat().st_size <= 1024:
            raise GoldPackagingAcceptanceError(f"Reviewed thumbnail is unexpectedly small: {path.name}")

    thumbnail_rights = rights.get("thumbnails")
    if (
        not isinstance(thumbnail_rights, list)
        or len(thumbnail_rights) != 3
        or not all(isinstance(x, dict) for x in thumbnail_rights)
    ):
        raise GoldPackagingAcceptanceError("Rights manifest must contain exactly three thumbnail rights records")
    rights_files = [str(item.get("output_file") or "") for item in thumbnail_rights]
    if rights_files != list(EXPECTED_THUMBNAILS):
        raise GoldPackagingAcceptanceError("Thumbnail rights records do not bind to the delivered A/B/C files")

    derived_records = 0
    for candidate, item in zip(candidates, thumbnail_rights):
        provider = str(item.get("provider") or "").strip().lower()
        candidate_provider = str(candidate.get("photo_provider") or "").strip().lower()
        if candidate_provider and candidate_provider != provider:
            raise GoldPackagingAcceptanceError("Thumbnail plan provider does not match rights provenance")
        candidate_asset = candidate.get("photo_id")
        rights_asset = item.get("provider_asset_id")
        if candidate_asset not in (None, "") and str(candidate_asset) != str(rights_asset):
            raise GoldPackagingAcceptanceError("Thumbnail plan asset ID does not match rights provenance")

        if provider in _ALLOWED_STOCK_PROVIDERS:
            if not str(item.get("license_url") or "").strip():
                raise GoldPackagingAcceptanceError(f"Missing license URL for delivered {provider} thumbnail")
            if rights_asset in (None, ""):
                raise GoldPackagingAcceptanceError(f"Missing provider asset ID for delivered {provider} thumbnail")
            continue

        if provider != _DERIVED_FINAL_RENDER_PROVIDER:
            raise GoldPackagingAcceptanceError(f"Unsupported delivered thumbnail provider: {provider or 'missing'}")
        derived_records += 1
        if package.get("budget_degraded") is not True:
            raise GoldPackagingAcceptanceError("Final-render thumbnail derivatives require explicit budget_degraded package evidence")
        if str(item.get("source_file") or "") != "final.mp4":
            raise GoldPackagingAcceptanceError("Final-render thumbnail derivative must bind to final.mp4")
        if str(item.get("rights_inheritance") or "") != "rights-manifest.visuals":
            raise GoldPackagingAcceptanceError("Final-render thumbnail derivative lacks visual-rights inheritance")
        if int(item.get("inherited_visual_rights_count") or 0) < 1:
            raise GoldPackagingAcceptanceError("Final-render thumbnail derivative has no inherited visual rights")
        if rights_asset in (None, ""):
            raise GoldPackagingAcceptanceError("Final-render thumbnail derivative lacks timestamp provenance")

    if derived_records not in {0, 3}:
        raise GoldPackagingAcceptanceError("Packaging cannot mix stock thumbnails with budget-fallback final-render derivatives")
    if derived_records == 3:
        fallback = package.get("budget_fallback")
        if not isinstance(fallback, dict) or fallback.get("provider_attempts_consumed") != 0:
            raise GoldPackagingAcceptanceError("Budget-fallback packaging must prove zero thumbnail provider attempts")
        if rights.get("thumbnail_rights_mode") != "derived_from_already_rights_cleared_final_render":
            raise GoldPackagingAcceptanceError("Budget-fallback thumbnail rights mode is missing or inconsistent")


def _source_bindings(root: Path, profile: str) -> dict[str, dict[str, Any]]:
    required = {
        "final": root / "final.mp4",
        "plan": root / "plan.json",
        "thumbnail_plan": root / "thumbnail-plan.json",
        "rights_manifest": root / "rights-manifest.json",
        "final_critic": root / "final-critic.json",
        "thumbnail_1": root / EXPECTED_THUMBNAILS[0],
        "thumbnail_2": root / EXPECTED_THUMBNAILS[1],
        "thumbnail_3": root / EXPECTED_THUMBNAILS[2],
    }
    qc = root / "final-master-qc.json"
    if qc.is_file() and not qc.is_symlink():
        required["final_master_qc"] = qc
    return {key: _regular_file_binding(path) for key, path in required.items()}


def seal_gold_packaging_acceptance(output_dir: Path, *, critic: dict[str, Any]) -> dict[str, Any]:
    """Seal P5 before state acceptance, binding the exact package bytes Gold reviewed."""
    root = Path(output_dir)
    if critic.get("status") != "pass":
        raise GoldPackagingAcceptanceError("Gold packaging acceptance requires a passing final critic")
    critic_path = root / "final-critic.json"
    if not critic_path.exists():
        _atomic_json(critic_path, critic)
    package = _read_object(root / "thumbnail-plan.json")
    rights = _read_object(root / "rights-manifest.json")
    profile = _profile_from_package(package)
    short_frame_selection: list[dict[str, Any]] = []
    if profile == "short_truthful_frame_selection":
        short_frame_selection = _ensure_short_selection_frames(root)
    _validate_base_package(root, package, rights, profile)
    document = {
        "contract_id": CONTRACT_ID,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "decision": "pass",
        "production_stage": "gold_packaging_pre_state_acceptance",
        "profile": profile,
        "policy_fingerprint": packaging_policy_fingerprint(),
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip().lower() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower() or None,
        "sources": _source_bindings(root, profile),
        "short_frame_selection": short_frame_selection,
        "publication_mode": "manual_in_youtube_studio",
    }
    _atomic_json(root / ACCEPTANCE_FILENAME, document)
    return document


def require_gold_packaging_acceptance(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    document = _read_object(root / ACCEPTANCE_FILENAME)
    if document.get("contract_id") != CONTRACT_ID:
        raise GoldPackagingAcceptanceError("Gold packaging contract ID mismatch")
    if int(document.get("schema_version") or 0) != CONTRACT_SCHEMA_VERSION:
        raise GoldPackagingAcceptanceError("Gold packaging contract schema mismatch")
    if document.get("decision") != "pass":
        raise GoldPackagingAcceptanceError("Gold packaging acceptance is not passing")
    if document.get("production_stage") != "gold_packaging_pre_state_acceptance":
        raise GoldPackagingAcceptanceError("Gold packaging acceptance stage mismatch")
    if document.get("publication_mode") != "manual_in_youtube_studio":
        raise GoldPackagingAcceptanceError("Gold packaging acceptance changed publication policy")
    if document.get("policy_fingerprint") != packaging_policy_fingerprint():
        raise GoldPackagingAcceptanceError("Gold packaging acceptance policy drift")
    profile = str(document.get("profile") or "")
    if profile not in {"long_title_thumbnail_hypothesis_set", "short_truthful_frame_selection"}:
        raise GoldPackagingAcceptanceError("Gold packaging acceptance profile is invalid")
    expected = _source_bindings(root, profile)
    if document.get("sources") != expected:
        raise GoldPackagingAcceptanceError("Gold packaging exact-artifact identity mismatch")
    package = _read_object(root / "thumbnail-plan.json")
    rights = _read_object(root / "rights-manifest.json")
    if _profile_from_package(package) != profile:
        raise GoldPackagingAcceptanceError("Gold packaging profile no longer matches thumbnail plan")
    _validate_base_package(root, package, rights, profile)
    if profile == "short_truthful_frame_selection":
        evidence = document.get("short_frame_selection")
        if not isinstance(evidence, list) or len(evidence) != 3:
            raise GoldPackagingAcceptanceError("Short packaging acceptance is missing frame-selection provenance")
        if [str(item.get("file") or "") for item in evidence if isinstance(item, dict)] != list(EXPECTED_THUMBNAILS):
            raise GoldPackagingAcceptanceError("Short frame-selection provenance does not bind the release files")
        if any(str(item.get("source_file") or "") != "final.mp4" for item in evidence if isinstance(item, dict)):
            raise GoldPackagingAcceptanceError("Short frame-selection provenance escaped final.mp4")
    return document


def gold_packaging_acceptance_sha256(output_dir: Path) -> str:
    require_gold_packaging_acceptance(output_dir)
    return _sha256_file(Path(output_dir) / ACCEPTANCE_FILENAME)


def validate_packaging_delivery(root: Path) -> dict[str, Path]:
    """Validate the exact Gold package, its rights, and its sealed P5 receipt before release."""
    root = Path(root)
    if not root.is_dir():
        raise GoldPackagingAcceptanceError(f"Packaging delivery root is not a directory: {root}")

    acceptance = require_gold_packaging_acceptance(root)
    profile = str(acceptance["profile"])
    thumbnail_plan = root / "thumbnail-plan.json"
    rights_manifest = root / "rights-manifest.json"
    gold_report = root / "gold-enforce-report.json"
    acceptance_path = root / ACCEPTANCE_FILENAME

    package = _read_object(thumbnail_plan)
    rights = _read_object(rights_manifest)
    _validate_base_package(root, package, rights, profile)

    gold = _read_object(gold_report)
    gold_decision = gold.get("gold")
    same_render = gold.get("same_render")
    if gold.get("phase") != "4" or gold.get("mode") != "enforce" or gold.get("release_authority") != "gold":
        raise GoldPackagingAcceptanceError("Packaging delivery requires the enforced Gold Phase 4 report")
    if gold.get("single_render") is not True:
        raise GoldPackagingAcceptanceError("Packaging delivery requires the Gold single-render invariant")
    if not isinstance(gold_decision, dict) or gold_decision.get("accepted") is not True:
        raise GoldPackagingAcceptanceError("Packaging delivery requires Gold acceptance")
    if not isinstance(same_render, dict) or same_render.get("artifact_divergence") is not False:
        raise GoldPackagingAcceptanceError("Packaging delivery blocked because final render diverged during Gold enforcement")

    report_acceptance = gold.get("packaging_acceptance")
    if not isinstance(report_acceptance, dict):
        raise GoldPackagingAcceptanceError("Gold report is missing the P5 packaging acceptance binding")
    if report_acceptance.get("contract_id") != CONTRACT_ID or report_acceptance.get("profile") != profile:
        raise GoldPackagingAcceptanceError("Gold report packaging acceptance metadata mismatch")
    if report_acceptance.get("certificate_file") != ACCEPTANCE_FILENAME:
        raise GoldPackagingAcceptanceError("Gold report packaging acceptance filename mismatch")
    if report_acceptance.get("certificate_sha256") != _sha256_file(acceptance_path):
        raise GoldPackagingAcceptanceError("Gold report packaging acceptance SHA mismatch")
    if report_acceptance.get("embedded_certificate") != acceptance:
        raise GoldPackagingAcceptanceError("Gold report embedded packaging certificate mismatch")

    return {
        "thumbnail_plan": thumbnail_plan,
        "rights_manifest": rights_manifest,
        "gold_report": gold_report,
        "gold_packaging_acceptance": acceptance_path,
        "thumbnail_1": root / EXPECTED_THUMBNAILS[0],
        "thumbnail_2": root / EXPECTED_THUMBNAILS[1],
        "thumbnail_3": root / EXPECTED_THUMBNAILS[2],
    }
