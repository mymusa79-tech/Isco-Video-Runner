from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


STAGE_ID = "final_cut_visual_qa"


class CleanV2VisualQABlock(RuntimeError):
    pass


class CleanV2VisualQAInfrastructure(RuntimeError):
    pass


def _secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    file_name = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _infrastructure_error(exc: BaseException) -> bool:
    try:
        from scripts.vision_provider_reliability import VisionProviderMeshUnavailableError
    except Exception:
        VisionProviderMeshUnavailableError = ()  # type: ignore[assignment]
    if VisionProviderMeshUnavailableError and isinstance(exc, VisionProviderMeshUnavailableError):
        return True

    try:
        from scripts.vision_stage_contract_v2 import VisionErrorCode, VisionStageError

        if isinstance(exc, VisionStageError):
            return exc.code is not VisionErrorCode.INTERNAL_CONTRACT_ERROR
    except Exception:
        pass

    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "provider mesh unavailable",
            "rate limit",
            "rate_limit",
            "quota",
            "resource_exhausted",
            "http_429",
            "timeout",
            "timed out",
            "model not found",
            "missing_api_key",
            "api key",
        )
    )


def run_final_cut_visual_qa(
    *,
    output_dir: Path,
    plan: dict[str, Any],
    script: dict[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
) -> dict[str, Any]:
    """Review only the clips already selected by Clean V2.

    This stage does not search, repair, replace, or re-render anything. It restores the
    existing Engine visual-audit semantic gate over the exact selected clips and emits
    canonical visual-audit.json evidence required by later Gold evaluation.
    """

    from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
    from isco_video_agent.media.ffmpeg import make_review_preview
    import isco_video_agent.orchestrator as orchestrator
    from isco_video_agent.providers.gemini import audit_video_preview
    from isco_video_agent.visual_selection import (
        FINAL_CUT_TARGET_SEMANTIC_FLOOR,
        is_final_cut_ready,
        semantic_floor,
    )
    from scripts.run181_vision_mesh_closure import install_run181_vision_mesh_closure
    from scripts.vision_provider_reliability import vision_provider_circuit_scope
    from scripts.vision_stage_contract_v2 import (
        VisionStageError,
        install_vision_provider_reliability,
    )

    output_dir = Path(output_dir)
    sections = list(plan.get("sections") or [])
    script_by_id = {
        str(item.get("id") or ""): str(item.get("narration") or "")
        for item in (script.get("sections") or [])
        if isinstance(item, dict)
    }
    right_by_section: dict[str, list[dict[str, Any]]] = {}
    for row in rights:
        if not isinstance(row, dict):
            continue
        section_id = str(row.get("section_id") or "").strip()
        right_by_section.setdefault(section_id, []).append(row)

    expected_ids = [str(item.get("id") or "").strip() for item in sections]
    if (
        not expected_ids
        or any(not section_id for section_id in expected_ids)
        or len(expected_ids) != len(set(expected_ids))
        or set(right_by_section) != set(expected_ids)
        or any(len(right_by_section.get(section_id, [])) != 1 for section_id in expected_ids)
    ):
        raise CleanV2VisualQABlock(
            "CLEAN_V2_VISUAL_QA_BLOCK reason=selected_visual_section_coverage_mismatch"
        )

    gemini = _secret("GEMINI_API_KEY")
    model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
    if not gemini:
        raise CleanV2VisualQAInfrastructure(
            "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE reason=gemini_api_key_missing"
        )

    install_vision_provider_reliability()
    install_run181_vision_mesh_closure()

    ledger = BudgetLedger(fmt, enforce=True)
    audits: list[dict[str, Any]] = []
    preview_dir = output_dir / "visual-qa"
    preview_dir.mkdir(parents=True, exist_ok=True)

    try:
        with vision_provider_circuit_scope():
            for index, section in enumerate(sections, start=1):
                section_id = str(section.get("id") or "").strip()
                row = right_by_section[section_id][0]
                local_file = str(row.get("local_file") or "").strip()
                clip = output_dir / "visuals" / local_file
                if not local_file or not clip.is_file():
                    raise CleanV2VisualQABlock(
                        f"CLEAN_V2_VISUAL_QA_BLOCK section={section_id} reason=selected_visual_missing"
                    )

                preview = preview_dir / f"{index:02d}-{section_id}-preview.mp4"
                make_review_preview(
                    clip,
                    preview,
                    portrait=fmt in {"moment", "story"},
                )
                narration_context = script_by_id.get(section_id, "")
                intended_visual = str(section.get("visual_query_en") or "").strip()
                spec = TaskSpec(
                    task_id=f"CLEAN_V2_VISUAL_AUDIT_S{index:02d}",
                    kind="VISUAL_AUDIT",
                    priority=Priority.P0,
                    capability=Capability.VISION,
                    max_provider_attempts=5,
                    schema_repair_allowed=False,
                    local_fallback=False,
                    semantic_block_is_final=True,
                )
                try:
                    raw = orchestrator._ledger_call_status(
                        ledger,
                        spec,
                        "gemini",
                        model,
                        audit_video_preview,
                        gemini,
                        preview,
                        narration_context=narration_context,
                        intended_visual=intended_visual,
                        model=model,
                    )
                except Exception as exc:
                    if isinstance(exc, VisionStageError):
                        _write_json(
                            output_dir / "visual-qa-diagnostics.json",
                            {
                                "schema_version": 1,
                                "stage": STAGE_ID,
                                "section": section_id,
                                "error_type": type(exc).__name__,
                                "error_code": exc.code.value,
                                "provider": exc.provider,
                                "requested_model": exc.requested_model,
                                "resolved_model": exc.resolved_model,
                                "http_status": exc.http_status,
                                "http_message": exc.http_message,
                                "detail": exc.detail,
                            },
                        )
                    if _infrastructure_error(exc):
                        raise CleanV2VisualQAInfrastructure(
                            f"CLEAN_V2_VISUAL_QA_INFRASTRUCTURE section={section_id} "
                            f"error_type={type(exc).__name__}"
                        ) from exc
                    raise CleanV2VisualQABlock(
                        f"CLEAN_V2_VISUAL_QA_BLOCK section={section_id} "
                        f"reason=visual_audit_contract_error error_type={type(exc).__name__}"
                    ) from exc

                audit = dict(raw)
                floor = semantic_floor(audit)
                audit.update(
                    {
                        "section": section_id,
                        "provider": str(row.get("provider") or ""),
                        "candidate_id": row.get("asset_id"),
                        "from_cache": False,
                        "intended_visual": intended_visual,
                        "fit_score_10": round(floor * 10.0, 3),
                        "review_origin": "clean_v2_selected_clip_cloud_visual_qa",
                        "vision_review_performed": True,
                        "is_selected": True,
                        "is_final_cut_auxiliary": False,
                        "final_cut_semantic_floor": round(floor, 6),
                        "final_cut_readiness_target": FINAL_CUT_TARGET_SEMANTIC_FLOOR,
                        "final_cut_readiness": (
                            "ready" if is_final_cut_ready(audit) else "not_ready"
                        ),
                    }
                )
                audits.append(audit)
                _write_json(output_dir / "visual-audit.json", audits)

                if not is_final_cut_ready(audit):
                    raise CleanV2VisualQABlock(
                        f"CLEAN_V2_VISUAL_QA_BLOCK section={section_id} "
                        f"reason=selected_visual_not_final_cut_ready "
                        f"status={audit.get('status')} floor={floor:.6f}"
                    )
    finally:
        ledger.write(output_dir / "visual-qa-budget.json")

    report = {
        "schema_version": 1,
        "layer": STAGE_ID,
        "status": "pass",
        "mode": "selected_clips_only",
        "repair_or_replacement_enabled": False,
        "section_count": len(expected_ids),
        "audited_selected_clip_count": len(audits),
        "final_cut_readiness_target": FINAL_CUT_TARGET_SEMANTIC_FLOOR,
        "provider_attempts": ledger.to_summary().get("provider_attempts", {}),
        "final_media_mutated": False,
    }
    _write_json(output_dir / "final-cut-visual-qa.json", report)
    return report
