from __future__ import annotations

"""Format-aware body-duration truth for Final Master QC.

Long-form renders own an M7 ``visual-timeline.json`` and keep that artifact mandatory.
Moment/Short renders do not own an M7 timeline before the Short finishing seam. Their
base/final render duration is measured in ``quality-final.json``; standalone cinematic
Shorts may additionally own ``short-visual-timeline.json`` after finishing.

This module resolves those format-specific authorities without weakening Final Master QC.
If an authority exists but is malformed or disagrees materially with another authoritative
measurement, resolution fails closed instead of silently falling back.
"""

import json
from pathlib import Path
from typing import Any


DURATION_CONSISTENCY_TOLERANCE_SECONDS = 0.25


class FinalMasterBodyContractError(RuntimeError):
    pass


def _read_required_object(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink():
            raise ValueError("symlink not allowed")
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise FinalMasterBodyContractError(
            f"Missing or invalid final-master QC source: {path.name}"
        ) from exc
    if not isinstance(data, dict):
        raise FinalMasterBodyContractError(
            f"Final-master QC source must be an object: {path.name}"
        )
    return data


def _read_optional_object(path: Path) -> dict[str, Any] | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_required_object(path)


def _positive_duration(data: dict[str, Any], *keys: str) -> tuple[float, str] | None:
    for key in keys:
        try:
            value = float(data.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, key
    return None


def _assert_consistent(left: float, right: float, *, left_name: str, right_name: str) -> None:
    if abs(left - right) <= DURATION_CONSISTENCY_TOLERANCE_SECONDS:
        return
    raise FinalMasterBodyContractError(
        "Final Master QC body-duration authority mismatch: "
        f"{left_name}={left:.3f}s {right_name}={right:.3f}s"
    )


def resolve_body_contract(
    output_dir: Path,
    *,
    fmt: str,
    quality: dict[str, Any],
) -> dict[str, Any]:
    """Resolve body duration from the artifact actually owned by this render format."""
    root = Path(output_dir)
    normalized = str(fmt or "").strip().lower()

    if normalized in {"film", "story"}:
        timeline = _read_required_object(root / "visual-timeline.json")
        resolved = _positive_duration(timeline, "duration_seconds")
        if resolved is None:
            raise FinalMasterBodyContractError(
                "Final Master QC requires positive M7 timeline duration"
            )
        duration, _key = resolved
        return {
            "kind": "long_m7_timeline",
            "duration_seconds": duration,
            "source": "visual-timeline.json:duration_seconds",
            "m7_timeline_authoritative": True,
            "short_timeline_authoritative": False,
            "quality_duration_crosscheck_seconds": None,
        }

    if normalized != "moment":
        raise FinalMasterBodyContractError("Final Master QC received unsupported format")

    quality_resolved = _positive_duration(
        quality,
        "video_stream_duration",
        "duration_seconds",
        "duration",
    )
    if quality_resolved is None:
        raise FinalMasterBodyContractError(
            "Final Master QC requires positive measured Moment duration in quality-final.json"
        )
    quality_duration, quality_key = quality_resolved

    short_timeline = _read_optional_object(root / "short-visual-timeline.json")
    if short_timeline is None:
        return {
            "kind": "moment_measured_render",
            "duration_seconds": quality_duration,
            "source": f"quality-final.json:{quality_key}",
            "m7_timeline_authoritative": False,
            "short_timeline_authoritative": False,
            "quality_duration_crosscheck_seconds": quality_duration,
        }

    short_resolved = _positive_duration(short_timeline, "duration_seconds")
    if short_resolved is None:
        raise FinalMasterBodyContractError(
            "Final Master QC requires positive short cinematic timeline duration"
        )
    short_duration, _key = short_resolved
    _assert_consistent(
        short_duration,
        quality_duration,
        left_name="short-visual-timeline.json:duration_seconds",
        right_name=f"quality-final.json:{quality_key}",
    )
    return {
        "kind": "moment_short_cinematic_timeline",
        "duration_seconds": short_duration,
        "source": "short-visual-timeline.json:duration_seconds",
        "m7_timeline_authoritative": False,
        "short_timeline_authoritative": True,
        "quality_duration_crosscheck_seconds": quality_duration,
    }
