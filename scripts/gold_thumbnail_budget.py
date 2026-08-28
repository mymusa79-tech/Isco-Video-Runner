from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
import re

import isco_video_agent.media.ffmpeg as media_ffmpeg
import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from isco_video_agent.orchestrator import _ledger_call, _ledger_call_status

from scripts.run123_budget_closure import remaining_priority_capacity


_PREVIEW_RE = re.compile(r"(?P<concept>\d+)-preview-(?P<attempt>\d+)\.jpg$")
_GOLD_THUMBNAIL_PROVIDER_ATTEMPTS = 4
_FALLBACK_FRAME_FRACTIONS = (0.18, 0.50, 0.82)


def _concept_spec() -> TaskSpec:
    return TaskSpec(
        task_id="GOLD_SHADOW_THUMBNAIL_CONCEPTS",
        kind="GOLD_SHADOW_THUMBNAIL_CONCEPTS",
        priority=Priority.P2,
        capability=Capability.TEXT,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _visual_spec(preview: Path) -> TaskSpec:
    match = _PREVIEW_RE.search(Path(preview).name)
    if match:
        suffix = f"C{int(match.group('concept')):02d}_A{int(match.group('attempt')):02d}"
    else:
        # Defensive deterministic fallback for future Packaging layouts. The preview
        # filename is never sent to a provider; it only names the logical ledger task.
        safe = re.sub(r"[^A-Za-z0-9]+", "_", Path(preview).stem).strip("_")[:48] or "UNKNOWN"
        suffix = safe.upper()
    return TaskSpec(
        task_id=f"GOLD_SHADOW_THUMBNAIL_VISUAL_{suffix}",
        kind="GOLD_SHADOW_THUMBNAIL_VISUAL",
        priority=Priority.P2,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


def _package_specs() -> list[TaskSpec]:
    return [_concept_spec()] + [
        _visual_spec(Path(f"{index}-preview-1.jpg")) for index in range(1, 4)
    ]


def _register_budget_skipped_package(ledger: BudgetLedger) -> None:
    # Registration without record_attempt() makes the four skipped enhancement tasks
    # visible in ai-budget.json's p2_skipped evidence while consuming zero attempts.
    for spec in _package_specs():
        ledger.register_task(spec)


def _title_options(plan) -> list[str]:
    values: list[str] = []
    for raw in list(getattr(plan, "title_options", []) or []):
        title = " ".join(str(raw or "").strip().split())
        if title and title not in values:
            values.append(title[:220])
        if len(values) == 3:
            break
    topic = " ".join(str(getattr(plan, "topic", "") or "").strip().split()) or "نداء اليقظة"
    while len(values) < 3:
        values.append(topic[:220])
    return values[:3]


def _extract_fallback_frame(final_path: Path, output: Path, timestamp: float) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
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
        raise RuntimeError("Gold P2 fallback could not extract a usable final-render frame")
    if output.stat().st_size > 2 * 1024 * 1024:
        media_ffmpeg._run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-q:v",
                "6",
                str(output.with_suffix(".compressed.jpg")),
            ]
        )
        compressed = output.with_suffix(".compressed.jpg")
        if not compressed.is_file() or compressed.stat().st_size > 2 * 1024 * 1024:
            raise RuntimeError("Gold P2 fallback thumbnail exceeds the 2 MB upload guard")
        compressed.replace(output)
    return output


def _build_final_render_fallback_package(
    *,
    plan,
    output_dir: Path,
    available_p2_attempts: int,
) -> dict:
    """Create A/B/C packaging from already-approved final-render frames with zero AI.

    This path is entered ONLY when the enforcing ledger cannot reserve all four Gold
    Thumbnail P2 calls before the package starts. It introduces no new stock asset:
    every frame comes from final.mp4 after core Vision selection and Final Master QC.
    Rights therefore inherit from the exact final render's already-recorded visual
    sources rather than pretending a new Pexels/Pixabay acquisition occurred.
    """
    root = Path(output_dir)
    final_path = root / "final.mp4"
    if not final_path.is_file():
        raise RuntimeError("Gold P2 fallback requires the exact existing final.mp4")
    total_duration = float(media_ffmpeg.duration(final_path))
    if total_duration <= 0:
        raise RuntimeError("Gold P2 fallback could not determine final render duration")

    titles = _title_options(plan)
    candidates: list[dict] = []
    for index, (fraction, title) in enumerate(zip(_FALLBACK_FRAME_FRACTIONS, titles), 1):
        timestamp = min(max(0.25, total_duration * fraction), max(0.25, total_duration - 0.25))
        filename = f"thumbnail-{index}.jpg"
        _extract_fallback_frame(final_path, root / filename, timestamp)
        slot = chr(ord("A") + index - 1)
        candidates.append(
            {
                "candidate_id": f"budget-fallback-{slot.lower()}",
                "experiment_slot": slot,
                "hypothesis_type": "final_render_frame_fallback",
                "title_ar": title,
                "text_mode": "none",
                "text_ar": "",
                "text_position": "center",
                "packaging_hypothesis": "Deterministic frame from the already-approved final edit; no extra AI packaging call.",
                "viewer_promise": "Inherited from the approved episode plan and final render.",
                "title_role": "Uses an existing approved title option.",
                "thumbnail_role": "Shows truthful footage already present in the final video.",
                "angle": "budget-safe final-render derivative",
                "why_it_can_work": "Preserves truthful visual continuity when optional Gold packaging AI is unavailable.",
                "file": filename,
                "mobile_preview_file": None,
                "photo_provider": "derived_final_render",
                "photo_id": f"final.mp4@{timestamp:.3f}s",
                "photo_url": None,
                "photographer": None,
                "photographer_url": None,
                "license_url": None,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "source_file": "final.mp4",
                "source_timestamp_seconds": round(timestamp, 3),
                "rights_inheritance": "rights-manifest.visuals",
                "visual_review_strategy": "inherited_from_core_final_cut_vision",
                "reviewed_candidate_count": 0,
                "reviewed_providers": [],
                "local_media_quarantine_count": 0,
                "local_media_quarantines": [],
                "visual_audit": {
                    "status": "pass",
                    "decision_source": "already_approved_final_cut",
                    "new_cloud_vision_performed": False,
                },
                "promise_integrity": {
                    "status": "review",
                    "reason": "P2 budget fallback preserves truthfulness but skips enhancement-only packaging optimization.",
                },
            }
        )

    return {
        "status": "ready",
        "packaging_contract_version": getattr(thumbnail, "PACKAGING_CONTRACT_VERSION", 2),
        "package_type": "title_thumbnail_hypothesis_set",
        "budget_degraded": True,
        "budget_fallback": {
            "reason": "p2_provider_attempt_capacity_exhausted",
            "required_provider_attempts": _GOLD_THUMBNAIL_PROVIDER_ATTEMPTS,
            "available_provider_attempts": int(available_p2_attempts),
            "provider_attempts_consumed": 0,
            "source": "exact_final_render_after_core_vision_and_master_qc",
        },
        "experiment_design": {
            "candidate_count": 3,
            "unit_of_comparison": "title_plus_thumbnail",
            "primary_objective": "qualified_watch_time",
            "hypothesis_types": ["final_frame_a", "final_frame_b", "final_frame_c"],
            "diversity_requirement": "three_distinct_final_render_timestamps",
            "native_execution": "manual_youtube_studio",
        },
        "visual_source_policy": {
            "primary": "derived_final_render",
            "fallback": None,
            "rights_inheritance": "existing final-cut visual rights",
        },
        "visual_review": {
            "strategy": "inherit_core_final_cut_vision",
            "max_candidates_per_hypothesis": 0,
            "max_vision_calls_per_package": 0,
            "planning_calls_per_package": 0,
            "max_ai_calls_per_package": 0,
        },
        "packaging_telemetry": {
            "provider_attempt_ceiling": 0,
            "budget_degraded": True,
            "p2_safe_skip": True,
        },
        "selection_rule": "Use the three truthful final-render derivatives as fallback A/B/C choices; human review remains allowed before manual YouTube upload.",
        "candidates": candidates,
    }


@contextmanager
def _budget_thumbnail_provider_calls(
    *,
    ledger: BudgetLedger,
    model: str,
) -> Iterator[None]:
    """Temporarily ledger the provider boundaries already owned by thumbnail.py.

    Packaging 360 remains the sole owner of thumbnail.py. This adapter does not copy
    its packaging logic and does not change its source file; it only wraps the two
    module-local Gemini callables during one synchronous Gold evaluation, then restores
    them in finally. Production is single-threaded at this boundary.
    """
    original_json_text = thumbnail.json_text
    original_audit_image_preview = thumbnail.audit_image_preview

    def budgeted_json_text(api_key: str, prompt: str, *, model: str):
        return _ledger_call(
            ledger,
            _concept_spec(),
            "gemini",
            model,
            original_json_text,
            api_key,
            prompt,
            model=model,
        )

    def budgeted_audit_image_preview(api_key: str, preview: Path, *args, **kwargs):
        resolved_model = str(kwargs.get("model") or model)
        return _ledger_call_status(
            ledger,
            _visual_spec(Path(preview)),
            "gemini",
            resolved_model,
            original_audit_image_preview,
            api_key,
            preview,
            *args,
            **kwargs,
        )

    thumbnail.json_text = budgeted_json_text
    thumbnail.audit_image_preview = budgeted_audit_image_preview
    try:
        yield
    finally:
        thumbnail.json_text = original_json_text
        thumbnail.audit_image_preview = original_audit_image_preview


def build_budgeted_thumbnail_package(
    *,
    gemini_key: str,
    pexels_key: str,
    plan,
    output_dir: Path,
    model: str,
    ledger: BudgetLedger,
    pixabay_key: str | None = None,
) -> dict:
    """Build normal Gold packaging or degrade safely before any partial P2 work starts."""
    if str(getattr(plan, "format", "")) == "moment":
        with _budget_thumbnail_provider_calls(ledger=ledger, model=model):
            return thumbnail.build_thumbnail_package(
                gemini_key=gemini_key,
                pexels_key=pexels_key,
                pixabay_key=pixabay_key,
                plan=plan,
                output_dir=output_dir,
                model=model,
            )

    available = remaining_priority_capacity(ledger, Priority.P2)
    enforcing = bool(getattr(ledger, "_enforce", False))
    if enforcing and available is not None and available < _GOLD_THUMBNAIL_PROVIDER_ATTEMPTS:
        _register_budget_skipped_package(ledger)
        print(
            "Gold Thumbnail P2 budget safe-skip: "
            f"required={_GOLD_THUMBNAIL_PROVIDER_ATTEMPTS} available={available}; "
            "using exact-final-render fallback with zero provider attempts"
        )
        return _build_final_render_fallback_package(
            plan=plan,
            output_dir=Path(output_dir),
            available_p2_attempts=available,
        )

    with _budget_thumbnail_provider_calls(ledger=ledger, model=model):
        return thumbnail.build_thumbnail_package(
            gemini_key=gemini_key,
            pexels_key=pexels_key,
            pixabay_key=pixabay_key,
            plan=plan,
            output_dir=output_dir,
            model=model,
        )
