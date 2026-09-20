from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from .media import probe_duration
from .visual_qa import (
    CleanV2VisualQABlock,
    CleanV2VisualQAInfrastructure,
    run_final_cut_visual_qa,
)


OPENING_WINDOW_SECONDS = 30.0
OPENING_COLD_OPEN_SECONDS = 7.0
OPENING_ESCALATION_SECONDS = 11.0
OPENING_PROMISE_SECONDS = 12.0
MAX_OPENING_AUXILIARY_CANDIDATES = 3


class CleanV2OpeningBlock(RuntimeError):
    pass


class CleanV2OpeningInfrastructure(RuntimeError):
    pass


def opening_slot_specs() -> list[dict[str, float | str]]:
    """Legacy M6 first-30-second split: 0-7, 7-18, 18-30."""
    return [
        {"key": "cold_open", "start": 0.0, "end": 7.0, "seconds": 7.0},
        {"key": "escalation", "start": 7.0, "end": 18.0, "seconds": 11.0},
        {"key": "promise_and_body", "start": 18.0, "end": 30.0, "seconds": 12.0},
    ]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_candidate_for_audit(candidate: Path, audit_root: Path) -> Path:
    visuals = audit_root / "visuals"
    visuals.mkdir(parents=True, exist_ok=True)
    destination = visuals / "candidate.mp4"
    shutil.copy2(candidate, destination)
    sidecar = candidate.with_suffix(".m8.json")
    if sidecar.is_file():
        shutil.copy2(sidecar, destination.with_suffix(".m8.json"))
    return destination


def _candidate_audit(
    *,
    audit_root: Path,
    candidate: Path,
    row: Mapping[str, Any],
    narration: str,
    query: str,
    fmt: str,
) -> dict[str, Any]:
    _copy_candidate_for_audit(candidate, audit_root)
    section_id = "opening_aux"
    mini_plan = {
        "sections": [
            {
                "id": section_id,
                "heading": "opening",
                "purpose": "opening auxiliary shot",
                "visual_query_en": query,
            }
        ]
    }
    mini_script = {
        "sections": [{"id": section_id, "narration": narration}]
    }
    mini_row = dict(row)
    mini_row["section_id"] = section_id
    mini_row["local_file"] = "candidate.mp4"

    report = run_final_cut_visual_qa(
        output_dir=audit_root,
        plan=mini_plan,
        script=mini_script,
        rights=[mini_row],
        fmt=fmt,
        router=None,
        visual_source=None,
    )
    audits_path = audit_root / "visual-audit.json"
    audits = json.loads(audits_path.read_text(encoding="utf-8"))
    selected = [
        item
        for item in audits
        if isinstance(item, dict) and item.get("is_selected") is True
    ]
    if not selected:
        raise CleanV2VisualQABlock("opening auxiliary audit has no selected verdict")
    return {
        "report": report,
        "audit": selected[-1],
    }


def _not_applicable(output_dir: Path, reason: str) -> dict[str, Any]:
    report = {
        "schema_version": 1,
        "layer": "opening_director",
        "status": "not_applicable",
        "reason": reason,
        "opening_window_seconds": OPENING_WINDOW_SECONDS,
        "slots": opening_slot_specs(),
    }
    _write_json(output_dir / "opening-director.json", report)
    return report


def run_opening_director(
    *,
    output_dir: Path,
    plan: Mapping[str, Any],
    script: Mapping[str, Any],
    rights: list[dict[str, Any]],
    fmt: str,
    narration_path: Path,
    visual_source: Any,
) -> dict[str, Any]:
    """Restore the old three-shot opening without changing the body visual policy.

    Film only: acquire up to three distinct safe stock candidates from section 1's
    already-approved query, reuse the exact current Final Cut Visual QA for each,
    and select two passing auxiliaries. The already-selected section-1 clip is the
    third audited shot and begins at 18s, continuing naturally into the body.
    """
    output_dir = Path(output_dir)
    if str(fmt) != "film":
        return _not_applicable(output_dir, "format_not_film")
    if not hasattr(visual_source, "acquire_replacement_candidates"):
        return _not_applicable(output_dir, "visual_source_has_no_bounded_candidate_api")

    duration = probe_duration(Path(narration_path))
    if duration < OPENING_WINDOW_SECONDS:
        return _not_applicable(output_dir, "narration_shorter_than_30_seconds")

    sections = list(plan.get("sections") or [])
    script_sections = list(script.get("sections") or [])
    if not sections or not script_sections:
        raise CleanV2OpeningBlock("CLEAN_V2_OPENING_BLOCK reason=opening_section_missing")

    first_id = str(sections[0].get("id") or "").strip()
    query = str(sections[0].get("visual_query_en") or "").strip()
    narration = str(script_sections[0].get("narration") or "").strip()
    primary_rows = [
        row for row in rights if str(row.get("section_id") or "").strip() == first_id
    ]
    if not first_id or not query or not narration or len(primary_rows) != 1:
        raise CleanV2OpeningBlock(
            "CLEAN_V2_OPENING_BLOCK reason=opening_primary_contract_invalid"
        )
    primary = primary_rows[0]
    primary_file = str(primary.get("local_file") or "").strip()
    primary_path = output_dir / "visuals" / primary_file
    if not primary_file or not primary_path.is_file():
        raise CleanV2OpeningBlock(
            "CLEAN_V2_OPENING_BLOCK reason=opening_primary_visual_missing"
        )

    main_qa_path = output_dir / "final-cut-visual-qa.json"
    main_audits_path = output_dir / "visual-audit.json"
    if not main_qa_path.is_file() or not main_audits_path.is_file():
        raise CleanV2OpeningBlock(
            "CLEAN_V2_OPENING_BLOCK reason=opening_primary_visual_not_audited"
        )
    main_qa = json.loads(main_qa_path.read_text(encoding="utf-8"))
    main_audits = json.loads(main_audits_path.read_text(encoding="utf-8"))
    primary_audited = any(
        isinstance(item, dict)
        and str(item.get("section") or "") == first_id
        and item.get("is_selected") is True
        and str(item.get("final_cut_readiness") or "") == "ready"
        for item in main_audits
    )
    if main_qa.get("status") != "pass" or not primary_audited:
        raise CleanV2OpeningBlock(
            "CLEAN_V2_OPENING_BLOCK reason=opening_primary_visual_not_final_cut_ready"
        )

    # The third shot starts at 18s and the ordinary body path then continues.
    # Require that the first ordinary body slot can cover the full 18-30 promise window.
    body_count = max(1, len(rights))
    if (duration - 18.0) / body_count < OPENING_PROMISE_SECONDS:
        return _not_applicable(output_dir, "body_slot_too_short_for_18_30_promise")

    exclusions = [
        (str(row.get("provider") or ""), row.get("asset_id"))
        for row in rights
        if isinstance(row, dict)
    ]
    before_events = len(getattr(visual_source, "events", []))
    candidates = visual_source.acquire_replacement_candidates(
        query,
        output_dir / "visuals",
        fmt,
        destination_name="opening-auxiliary.mp4",
        section_id=first_id,
        max_candidates=MAX_OPENING_AUXILIARY_CANDIDATES,
        exclude_assets=exclusions,
    )
    if len(candidates) < 2:
        recent_events = list(getattr(visual_source, "events", []))[before_events:]
        wired = any(bool(item.get("wire_attempted")) for item in recent_events if isinstance(item, dict))
        reason = "insufficient_stock_candidates_after_search" if wired else "opening_stock_providers_unavailable"
        raise CleanV2OpeningInfrastructure(
            f"CLEAN_V2_OPENING_INFRASTRUCTURE reason={reason}"
        )

    selected: list[tuple[Path, dict[str, Any], dict[str, Any], Path]] = []
    reviewed: list[dict[str, Any]] = []
    audit_root = output_dir / "opening-audits"
    for index, (candidate, row) in enumerate(candidates, start=1):
        candidate_root = audit_root / f"candidate-{index:02d}"
        try:
            verdict = _candidate_audit(
                audit_root=candidate_root,
                candidate=Path(candidate),
                row=row,
                narration=narration,
                query=query,
                fmt=fmt,
            )
        except CleanV2VisualQAInfrastructure as exc:
            raise CleanV2OpeningInfrastructure(
                f"CLEAN_V2_OPENING_INFRASTRUCTURE candidate={index} "
                f"error_type={type(exc).__name__}"
            ) from exc
        except CleanV2VisualQABlock as exc:
            reviewed.append(
                {
                    "candidate_index": index,
                    "provider": row.get("provider"),
                    "asset_id": row.get("asset_id"),
                    "status": "block",
                    "reason": str(exc)[:300],
                }
            )
            continue

        reviewed.append(
            {
                "candidate_index": index,
                "provider": row.get("provider"),
                "asset_id": row.get("asset_id"),
                "status": "pass",
                "final_cut_readiness": verdict["audit"].get("final_cut_readiness"),
                "fit_score_10": verdict["audit"].get("fit_score_10"),
            }
        )
        selected.append((Path(candidate), dict(row), verdict, candidate_root))
        if len(selected) == 2:
            break

    if len(selected) < 2:
        raise CleanV2OpeningBlock(
            "CLEAN_V2_OPENING_BLOCK reason=two_opening_auxiliaries_not_final_cut_ready"
        )

    slot_keys = ("cold_open", "escalation")
    auxiliary_rows: list[dict[str, Any]] = []
    auxiliary_files: list[str] = []
    for slot_key, (candidate, row, verdict, _candidate_root) in zip(slot_keys, selected):
        destination = output_dir / "visuals" / f"opening-{slot_key}.mp4"
        visual_source.commit_replacement(candidate, destination)
        row["local_file"] = destination.name
        row["section_id"] = first_id
        row["opening_slot"] = slot_key
        row["is_final_cut_auxiliary"] = True
        row["is_selected"] = False
        row["final_cut_readiness"] = verdict["audit"].get("final_cut_readiness")
        auxiliary_rows.append(row)
        auxiliary_files.append(destination.name)

    selected_candidates = {str(item[0]) for item in selected}
    for candidate, _row in candidates:
        candidate_path = Path(candidate)
        if str(candidate_path) in selected_candidates and not candidate_path.exists():
            continue
        candidate_path.unlink(missing_ok=True)
        candidate_path.with_suffix(".m8.json").unlink(missing_ok=True)

    slots = opening_slot_specs()
    slots[0]["local_file"] = auxiliary_files[0]
    slots[0]["audit_source"] = "opening-audits"
    slots[1]["local_file"] = auxiliary_files[1]
    slots[1]["audit_source"] = "opening-audits"
    slots[2]["local_file"] = primary_file
    slots[2]["audit_source"] = "final-cut-visual-qa"
    report = {
        "schema_version": 1,
        "layer": "opening_director",
        "status": "pass",
        "mode": "legacy_first_30_three_audited_shots",
        "opening_window_seconds": OPENING_WINDOW_SECONDS,
        "candidate_review_limit": MAX_OPENING_AUXILIARY_CANDIDATES,
        "candidate_review_count": len(reviewed),
        "audited_shot_count": 3,
        "primary_section_id": first_id,
        "body_continues_from_second": 18.0,
        "slots": slots,
        "candidate_reviews": reviewed,
    }
    _write_json(output_dir / "opening-director.json", report)
    _write_json(
        output_dir / "opening-rights.json",
        {
            "schema_version": 1,
            "assets": [*auxiliary_rows, dict(primary)],
        },
    )
    return report
