from __future__ import annotations

"""Final-cut Visual QA adapter for Clean V2.

This layer does not select, replace, repair, or re-search footage. Clean V2 has already
selected the clips. The adapter reuses the certified Engine visual audit normalizer,
the Runner Vision Stage V2/V3 provider mesh, and the existing final-cut semantic floor
to decide whether each exact selected clip is admissible as final-cut evidence.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

LAYER_ID = "final_cut_visual_qa_v1"
REPORT_NAME = "visual-qa-report.json"
AUDIT_NAME = "visual-audit.json"
BUDGET_NAME = "visual-qa-budget.json"


class CleanV2VisualQABlock(RuntimeError):
    pass


class CleanV2VisualQAInfrastructure(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lookup(items: Mapping[str, Any], key: str) -> dict[str, dict[str, Any]]:
    rows = items.get(key) if isinstance(items, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {
        str(row.get("id") or ""): row
        for row in rows
        if isinstance(row, dict) and str(row.get("id") or "")
    }


def _provider_failure_is_infrastructure(exc: BaseException) -> bool:
    try:
        from scripts import vision_provider_reliability as legacy
        from scripts.vision_stage_contract_v2 import VisionErrorCode, VisionStageError
    except Exception:
        return False

    if isinstance(exc, legacy.VisionProviderMeshUnavailableError):
        return True
    if isinstance(exc, VisionStageError):
        return exc.code is not VisionErrorCode.INTERNAL_CONTRACT_ERROR
    return False


def _persist(
    output_dir: Path,
    *,
    audits: list[dict[str, Any]],
    ledger: Any,
    status: str,
    blocked_section: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    (output_dir / AUDIT_NAME).write_text(
        json.dumps(audits, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    budget = ledger.to_summary()
    (output_dir / BUDGET_NAME).write_text(
        json.dumps(budget, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = {
        "schema_version": 1,
        "layer": LAYER_ID,
        "status": status,
        "selected_clips_reviewed": len(audits),
        "blocked_section": blocked_section,
        "reason": reason,
        "selection_mutated": False,
        "repair_or_research_added": False,
        "provider_order": ["gemini", "groq", "openrouter"],
        "semantic_policy": "engine.visual_audit_normalizer.v1",
        "final_cut_readiness_target": 0.85,
        "audit_file": AUDIT_NAME,
        "budget_file": BUDGET_NAME,
    }
    (output_dir / REPORT_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def apply_final_cut_visual_qa(
    *,
    output_dir: Path,
    clips: list[Path],
    rights: list[dict[str, Any]],
    plan: dict[str, Any],
    script: dict[str, Any],
    fmt: str,
) -> dict[str, Any]:
    """Audit exactly the selected final-cut clips; never select replacements."""

    output_dir = Path(output_dir)
    if not clips or len(clips) != len(rights):
        raise CleanV2VisualQABlock(
            "CLEAN_V2_VISUAL_QA_BLOCK reason=clip_rights_cardinality_mismatch"
        )

    try:
        import isco_video_agent.orchestrator as orchestrator
        from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
        from isco_video_agent.media.ffmpeg import make_review_preview
        from isco_video_agent.providers.gemini import audit_video_preview
        from isco_video_agent.visual_selection import (
            FINAL_CUT_TARGET_SEMANTIC_FLOOR,
            is_final_cut_ready,
            semantic_floor,
        )
        from scripts.run181_vision_mesh_closure import install_run181_vision_mesh_closure
        from scripts.vision_stage_contract_v2 import install_vision_provider_reliability
        from scripts import vision_provider_reliability as legacy
    except Exception as exc:
        raise CleanV2VisualQABlock(
            f"CLEAN_V2_VISUAL_QA_BLOCK reason=owner_import_failed type={type(exc).__name__}"
        ) from exc

    gemini = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if not gemini:
        raise CleanV2VisualQAInfrastructure(
            "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE reason=gemini_api_key_missing"
        )
    content_model = (
        str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
        or "gemini-3.7-flash"
    )

    install_vision_provider_reliability()
    install_run181_vision_mesh_closure()

    ledger_format = "film" if fmt == "film" else "story"
    ledger = BudgetLedger(ledger_format, enforce=True)
    plan_sections = _lookup(plan, "sections")
    script_sections = _lookup(script, "sections")
    portrait = fmt in {"moment", "story"}
    audits: list[dict[str, Any]] = []
    preview_dir = output_dir / "visual-qa-previews"

    with legacy.vision_provider_circuit_scope():
        for index, (clip, right) in enumerate(zip(clips, rights), start=1):
            section_id = str(right.get("section_id") or "").strip()
            if not section_id:
                section_id = f"clean_v2_visual_{index:02d}"
            p = plan_sections.get(section_id, {})
            s = script_sections.get(section_id, {})
            intended_visual = str(
                p.get("visual_query_en") or right.get("query") or "selected final-cut visual"
            ).strip()
            narration_context = str(s.get("narration") or "").strip()

            preview = make_review_preview(
                Path(clip),
                preview_dir / f"{index:02d}-{section_id}.mp4",
                portrait=portrait,
            )
            spec = TaskSpec(
                task_id=f"CLEAN_V2_FINAL_CUT_VISUAL_QA_{index:02d}",
                kind="VISUAL_AUDIT",
                priority=Priority.P0,
                capability=Capability.VISION,
                max_provider_attempts=3,
                schema_repair_allowed=False,
                local_fallback=False,
                semantic_block_is_final=True,
            )
            try:
                raw = orchestrator._ledger_call_status(
                    ledger,
                    spec,
                    "gemini",
                    content_model,
                    audit_video_preview,
                    gemini,
                    preview,
                    narration_context=narration_context,
                    intended_visual=intended_visual,
                    model=content_model,
                )
            except Exception as exc:
                _persist(
                    output_dir,
                    audits=audits,
                    ledger=ledger,
                    status="infrastructure" if _provider_failure_is_infrastructure(exc) else "block",
                    blocked_section=section_id,
                    reason=type(exc).__name__,
                )
                if _provider_failure_is_infrastructure(exc):
                    raise CleanV2VisualQAInfrastructure(
                        "CLEAN_V2_VISUAL_QA_INFRASTRUCTURE "
                        f"section={section_id} type={type(exc).__name__}"
                    ) from exc
                raise CleanV2VisualQABlock(
                    "CLEAN_V2_VISUAL_QA_BLOCK "
                    f"section={section_id} type={type(exc).__name__}"
                ) from exc

            audit = dict(raw)
            floor = semantic_floor(audit)
            audit.update(
                {
                    "section": section_id,
                    "candidate_id": right.get("asset_id"),
                    "provider": right.get("provider"),
                    "source_file": Path(clip).name,
                    "source_sha256": _sha256(Path(clip)),
                    "is_selected": True,
                    "is_final_cut_auxiliary": False,
                    "review_origin": "clean_v2_final_cut_visual_qa",
                    "vision_review_performed": True,
                    "final_cut_semantic_floor": round(float(floor), 6),
                    "final_cut_readiness_target": FINAL_CUT_TARGET_SEMANTIC_FLOOR,
                    "final_cut_readiness": (
                        "ready" if is_final_cut_ready(audit) else "not_ready"
                    ),
                }
            )
            audits.append(audit)

            if not is_final_cut_ready(audit):
                _persist(
                    output_dir,
                    audits=audits,
                    ledger=ledger,
                    status="block",
                    blocked_section=section_id,
                    reason="semantic_or_quality_floor_block",
                )
                raise CleanV2VisualQABlock(
                    "CLEAN_V2_VISUAL_QA_BLOCK "
                    f"section={section_id} status={audit.get('status')} "
                    f"semantic_floor={floor:.6f} target={FINAL_CUT_TARGET_SEMANTIC_FLOOR:.2f}"
                )

    return _persist(
        output_dir,
        audits=audits,
        ledger=ledger,
        status="pass",
    )
