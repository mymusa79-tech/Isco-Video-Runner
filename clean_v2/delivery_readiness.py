"""Post-render delivery evidence for the exact files a viewer/editor receives.

This observational sidecar never mutates final.mp4, does not create a new
production/provider stage, and never turns an optional cover or derivative
failure into a parent-video failure. Final Master QC remains the release gate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from PIL import Image

from .contracts import atomic_write_json

REPORT = "delivery-readiness.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for part in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _image(path: Path, fmt: str) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        return {"status": "missing", "file": path.name}
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            width, height = im.size
    except (OSError, ValueError, SyntaxError):
        return {"status": "unreadable", "file": path.name}
    ratio = width / max(1, height)
    expected = 9 / 16 if fmt == "short" else 16 / 9
    minimum = (720, 1280) if fmt == "short" else (1280, 720)
    valid = abs(ratio - expected) < 0.075 and width >= minimum[0] and height >= minimum[1]
    return {
        "status": "pass" if valid else "review",
        "file": path.name,
        "dimensions": [width, height],
        "issue": None if valid else "cover_aspect_or_mobile_resolution",
    }


def _existing(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def audit_delivery(output_dir: Path, *, fmt: str, expected_sha256: str = "") -> dict[str, Any]:
    """Review actual export and optional sidecars using only local evidence.

    A working master with a failed optional short is *partial_delivery*, not a
    failed parent production. The report distinguishes verified from inferred.
    """
    root = Path(output_dir)
    fmt = str(fmt)
    final = root / "final.mp4"
    qc = _read(root / "final-master-qc.json")
    notes: list[str] = []
    if _existing(final):
        actual_hash = _digest(final)
        hash_ok = not expected_sha256 or actual_hash == expected_sha256
        if not hash_ok:
            notes.append("final_export_sha256_changed_after_inspection")
    else:
        actual_hash, hash_ok = None, False
        notes.append("final_export_missing")
    if qc.get("status") != "pass":
        notes.append("final_master_qc_not_pass")
    primary_ready = bool(actual_hash and hash_ok and qc.get("status") == "pass")

    cover = _image(root / "cover.jpg", fmt)
    if cover["status"] != "pass":
        notes.append("primary_cover_" + cover["status"])

    original = _read(root / "production.json")
    # A generated fallback cover can meet pixel specs without meeting the
    # ChatGPT-first editorial requirement of a specifically designed image.
    # Warn instead of refusing older approved ZIPs whose cover.asset was null.
    authored_cover = original.get("cover")
    if isinstance(authored_cover, dict) and not authored_cover.get("asset"):
        notes.append("approved_cover_asset_not_in_zip")
    captions = _read(root / ("film-captions.json" if fmt == "film" else "podcast-captions.json"))
    if fmt in {"film", "podcast"}:
        sidecar = root / f"{fmt}-captions.srt"
        if captions.get("status") == "pass" and not _existing(sidecar):
            notes.append("spoken_srt_missing_after_success")
        elif captions.get("status") != "pass":
            notes.append("spoken_captions_not_confirmed")
    derivative = {"status": "not_applicable"}
    if fmt in {"film", "podcast"}:
        name = "long-short" if fmt == "film" else "podcast-short"
        candidate = original.get("derivative")
        if isinstance(candidate, dict):
            derivative_cover_plan = candidate.get("cover")
            if not isinstance(derivative_cover_plan, dict) or not derivative_cover_plan.get("asset"):
                notes.append("approved_derivative_cover_asset_not_in_zip")
        evidence = _read(root / "derivative-status.json")
        movie = root / f"{name}.mp4"
        selected = str(evidence.get("status") or "unreported")
        derived_file_present = _existing(movie)
        derived_qc: dict[str, Any] = {"status": "not_run"}
        if selected == "pass" and derived_file_present:
            try:
                # Re-probe the derived FINAL bytes, not the earlier temporary
                # extraction or a stale successful JSON report.
                from .media import inspect_final
                observed = inspect_final(movie)
                valid = (
                    (observed["width"], observed["height"]) == (1080, 1920)
                    and 7 <= float(observed["duration_seconds"]) <= 60.15
                )
                derived_qc = {
                    "status": "pass" if valid else "review",
                    "dimensions": [observed["width"], observed["height"]],
                    "duration_seconds": observed["duration_seconds"],
                    "sha256": observed["sha256"],
                }
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                derived_qc = {"status": "unreadable", "reason": type(exc).__name__}
        is_ready = selected == "pass" and derived_qc.get("status") == "pass"
        derivative = {
            "status": "pass" if is_ready else ("not_requested" if not candidate else "review"),
            "reported_status": selected,
            "actual_video_qc": derived_qc,
            "file": movie.name if derived_file_present else None,
            "expected_from_approved_zip": bool(candidate),
        }
        if candidate and not is_ready:
            notes.append("approved_derivative_not_delivered")
        if is_ready:
            derivative_cover = _image(root / f"{name}-cover.jpg", "short")
            derivative["cover"] = derivative_cover
            if derivative_cover["status"] != "pass":
                notes.append("derivative_cover_" + derivative_cover["status"])

    report = {
        "schema_version": 1, "format": fmt, "source": "post_render_actual_files",
        "status": ("review_master" if not primary_ready
                   else "complete" if not notes else "partial_delivery"),
        "primary_video_accepted": primary_ready,
        "final_file": final.name,
        "final_sha256": actual_hash,
        "sha256_matches_inspected_export": hash_ok,
        "final_master_qc_status": qc.get("status") or "unknown",
        "primary_cover": cover,
        "full_spoken_caption_status": captions.get("status") if fmt in {"film", "podcast"} else "format_owned",
        "derivative": derivative,
        "review_reasons": notes,
        "notes": "Deterministic file audit; not human visual/audio judgement or a YouTube retention forecast.",
        "provider_calls_added": 0, "primary_master_mutated": False,
        "new_release_gate": False,
    }
    atomic_write_json(root / REPORT, report)
    return report
