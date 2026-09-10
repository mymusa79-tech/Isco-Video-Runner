from __future__ import annotations

"""Deterministic viewer-quality release envelope for canonical V4 outputs.

This contract adds no model calls. It composes evidence already paid for by the
pipeline: Final Master QC, audio/A-V measurements, final-cut visual audits, short
cinematic pacing when applicable, and the enforcing Gold critic result.

The score is a 0-10 engineering release-confidence score, not a human MOS and not a
forecast of YouTube performance. A canonical release requires >=8.5 overall plus
non-compensable sub-gates. The contract is deliberately safe to run before production
state acceptance; it never mutates final.mp4 or publication state.
"""

import json
from pathlib import Path
from typing import Any

from scripts.final_master_acceptance_v2 import require_final_master_acceptance
from scripts.viewer_regression_run228 import enforce_run_228_viewer_regression


CONTRACT_ID = "viewer-quality.v1"
CONTRACT_VERSION = 1
FILENAME = "viewer-quality-contract.json"
MIN_VIEWER_SCORE = 8.5
MIN_VISUAL_SEMANTIC_SCORE = 8.5
MIN_SHORT_PACING_SCORE = 8.5


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_object(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise RuntimeError(f"Viewer Quality Contract requires JSON object: {path.name}")
    return value


def _bounded(value: float) -> float:
    return max(0.0, min(10.0, float(value)))


def _release_profile(root: Path, fmt: str) -> str:
    normalized = str(fmt or "").strip().lower()
    # Engine routing defines Film and Story as long-form outer episode shapes.
    # Only Moment is a Short. Keeping Story out of Short regression is critical:
    # Run #228 is a Moment reference and is not calibrated for long narrative arcs.
    if normalized in {"film", "story"}:
        return normalized
    try:
        plan = _read_object(root / "plan.json")
    except Exception:
        plan = {}
    source = str(plan.get("plan_source") or "").strip()
    if source == "source_derived_long_episode_video_short":
        return "derived_short"
    return "standalone_short"


def _final_cut_visual_records(root: Path) -> list[dict[str, Any]]:
    """Return only visual evidence that actually survived into the final cut.

    ``visual-audit.json`` is the canonical final-release visual evidence used by the
    Engine Final Critic. It contains both forensic candidate history and final-cut
    selections, so a generic ``status == pass`` filter would let rejected historical
    candidates inflate the release score. Keep the exact same authority boundary as
    the Final Critic: one selected section audit plus explicitly marked final-cut
    opening auxiliaries. Long multi-shot sections are represented by their selected
    composite audit whose relevance/quality floors are derived from every member.
    """
    path = root / "visual-audit.json"
    if not path.is_file():
        raise RuntimeError("Viewer Quality Contract requires visual-audit.json")
    payload = _read_json(path)
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("audits") or payload.get("results") or payload.get("entries") or ()
    else:
        raise RuntimeError("Viewer Quality Contract requires visual-audit evidence array")

    records: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        is_selected = item.get("is_selected") is True
        is_auxiliary = item.get("is_final_cut_auxiliary") is True
        if not is_selected and not is_auxiliary:
            continue
        if str(item.get("status") or "").lower() != "pass":
            raise RuntimeError("Viewer Quality Contract final-cut visual evidence did not pass")
        try:
            relevance = float(item.get("relevance"))
            quality = float(item.get("visual_quality"))
        except (TypeError, ValueError):
            raise RuntimeError("Viewer Quality Contract final-cut visual evidence lacks scores")
        if not (0.0 <= relevance <= 1.0 and 0.0 <= quality <= 1.0):
            raise RuntimeError("Viewer Quality Contract final-cut visual scores are out of range")
        records.append(
            {
                "source": "visual-audit.json",
                "section": item.get("section"),
                "provider": item.get("provider"),
                "candidate_id": item.get("candidate_id"),
                "final_cut_role": "selected_section" if is_selected else "auxiliary",
                "is_section_sequence": item.get("is_section_sequence") is True,
                "sequence_member_count": item.get("sequence_member_count"),
                "relevance": relevance,
                "visual_quality": quality,
                "semantic_floor": min(relevance, quality),
            }
        )
    return records


def _film_visual_coverage(root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Bind Film final-cut visual evidence exactly one-to-one to plan sections.

    Opening auxiliaries are real final-cut evidence and remain part of semantic
    scoring, but they are not substitutes for a plan section. Every ``sections[].id``
    must have exactly one canonical ``is_selected`` audit/composite, with no missing,
    duplicate, or unknown selected section ids.
    """
    plan = _read_object(root / "plan.json")
    raw_sections = plan.get("sections")
    if not isinstance(raw_sections, list):
        raw_sections = []

    expected: list[str] = []
    malformed_plan_sections = 0
    for section in raw_sections:
        if not isinstance(section, dict):
            malformed_plan_sections += 1
            continue
        section_id = str(section.get("id") or "").strip()
        if not section_id:
            malformed_plan_sections += 1
            continue
        expected.append(section_id)

    expected_counts: dict[str, int] = {}
    for section_id in expected:
        expected_counts[section_id] = expected_counts.get(section_id, 0) + 1

    selected = [item for item in records if item.get("final_cut_role") == "selected_section"]
    selected_ids: list[str] = []
    malformed_selected_records = 0
    invalid_sequences: list[str] = []
    for item in selected:
        section_id = str(item.get("section") or "").strip()
        if not section_id:
            malformed_selected_records += 1
            continue
        selected_ids.append(section_id)
        if item.get("is_section_sequence") is True:
            try:
                member_count = int(item.get("sequence_member_count"))
            except (TypeError, ValueError):
                invalid_sequences.append(section_id)
            else:
                if member_count < 2:
                    invalid_sequences.append(section_id)

    selected_counts: dict[str, int] = {}
    for section_id in selected_ids:
        selected_counts[section_id] = selected_counts.get(section_id, 0) + 1

    duplicate_plan_ids = sorted(section_id for section_id, count in expected_counts.items() if count != 1)
    duplicate_selected_ids = sorted(section_id for section_id, count in selected_counts.items() if count != 1)
    expected_unique = set(expected_counts)
    selected_unique = set(selected_counts)
    missing = sorted(expected_unique - selected_unique)
    unknown = sorted(selected_unique - expected_unique)
    matched = sum(
        1
        for section_id in expected_unique
        if expected_counts.get(section_id) == 1 and selected_counts.get(section_id) == 1
    )
    coverage_ratio = (matched / len(expected_unique)) if expected_unique else 0.0

    passed = (
        bool(expected_unique)
        and malformed_plan_sections == 0
        and malformed_selected_records == 0
        and not duplicate_plan_ids
        and not duplicate_selected_ids
        and not missing
        and not unknown
        and not invalid_sequences
        and coverage_ratio == 1.0
    )
    return {
        "contract": "film_plan_section_to_selected_final_cut_v1",
        "expected_section_count": len(expected_unique),
        "selected_section_record_count": len(selected),
        "matched_section_count": matched,
        "coverage_ratio": round(coverage_ratio, 4),
        "missing_section_ids": missing,
        "unknown_selected_section_ids": unknown,
        "duplicate_plan_section_ids": duplicate_plan_ids,
        "duplicate_selected_section_ids": duplicate_selected_ids,
        "malformed_plan_sections": malformed_plan_sections,
        "malformed_selected_records": malformed_selected_records,
        "invalid_sequence_section_ids": sorted(set(invalid_sequences)),
        "auxiliary_final_cut_records": sum(
            1 for item in records if item.get("final_cut_role") == "auxiliary"
        ),
        "pass": passed,
    }


def _visual_score(records: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
    if not records:
        raise RuntimeError("Viewer Quality Contract requires final-cut visual-audit evidence")
    semantic = [float(item["semantic_floor"]) for item in records]
    mean = sum(semantic) / len(semantic)
    weakest = min(semantic)
    score = _bounded(10.0 * mean)
    return score, {
        "final_cut_records": len(records),
        "semantic_mean": round(mean, 4),
        "semantic_min": round(weakest, 4),
        "score_10": round(score, 3),
        "minimum_required_score_10": MIN_VISUAL_SEMANTIC_SCORE,
        "weakest_final_visual_floor": 0.80,
        "coverage_semantics": "canonical_final_cut_selected_audits_only_v3",
        "pass": score >= MIN_VISUAL_SEMANTIC_SCORE and weakest >= 0.80,
    }


def _technical_score(qc: dict[str, Any], quality: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    blocking = list(qc.get("blocking_findings") or ())
    pass_flags = {
        "final_master_qc": str(qc.get("status") or "").lower() == "pass",
        "full_decode": qc.get("full_decode_ok") is True,
        "duration": quality.get("duration_ok") is True,
        "audio": quality.get("audio_ok") is True,
        "av_sync": quality.get("av_sync_ok") is True,
        "single_video_stream": int(quality.get("video_streams") or 0) == 1,
        "single_audio_stream": int(quality.get("audio_streams") or 0) == 1,
        "no_qc_blocking_findings": not blocking,
    }
    passed = sum(1 for ok in pass_flags.values() if ok)
    score = 10.0 * passed / len(pass_flags)
    return _bounded(score), {
        "checks": pass_flags,
        "score_10": round(_bounded(score), 3),
        "pass": all(pass_flags.values()),
    }


def _audio_score(quality: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    measurement = quality.get("audio_measurement") or {}
    try:
        measured_lufs = float(measurement.get("integrated_lufs"))
        target_lufs = float(quality.get("audio_target_lufs", measured_lufs))
        av_delta = abs(float(quality.get("av_delta_seconds", 999.0)))
    except (TypeError, ValueError):
        raise RuntimeError("Viewer Quality Contract requires measured loudness and A/V sync")
    lufs_delta = abs(measured_lufs - target_lufs)
    loudness_score = _bounded(10.0 - min(2.0, lufs_delta) * 0.75)
    if av_delta <= 0.05:
        sync_score = 10.0
    elif av_delta <= 0.10:
        sync_score = 9.5
    elif av_delta <= 0.20:
        sync_score = 8.5
    elif av_delta <= 0.30:
        sync_score = 7.5
    else:
        sync_score = 0.0
    score = 0.55 * loudness_score + 0.45 * sync_score
    return _bounded(score), {
        "integrated_lufs": measured_lufs,
        "target_lufs": target_lufs,
        "lufs_delta": round(lufs_delta, 3),
        "av_delta_seconds": round(av_delta, 4),
        "score_10": round(_bounded(score), 3),
        "pass": quality.get("audio_ok") is True and quality.get("av_sync_ok") is True,
    }


def _pacing_score(root: Path, fmt: str, profile: str, qc: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    # Only Moment uses Short visual-density pacing. Story is a long-form episode shape
    # and must be judged in narrative context rather than as a sequence of mini Shorts.
    if fmt != "moment":
        freeze_events = list(((qc.get("detectors") or {}).get("exact_freeze") or {}).get("events") or ())
        score = 10.0 if not freeze_events else 8.0
        return score, {
            "release_profile": profile,
            "format_class": "long",
            "exact_freeze_events": len(freeze_events),
            "score_10": score,
            "pass": not freeze_events,
            "long_specific_regression_calibration": "not_yet_calibrated",
        }

    timeline = _read_object(root / "short-visual-timeline.json")
    try:
        max_hold = float((timeline.get("visual_density_contract") or {}).get("actual_max_shot_hold_seconds"))
        shot_count = int(timeline.get("shot_count") or 0)
        semantic_beats = int(timeline.get("semantic_beat_count") or 0)
    except (TypeError, ValueError):
        raise RuntimeError("Viewer Quality Contract requires Short visual pacing evidence")

    if max_hold <= 4.5:
        hold_score = 10.0
    elif max_hold <= 6.0:
        hold_score = 10.0 - ((max_hold - 4.5) / 1.5)
    elif max_hold <= 7.0:
        hold_score = 9.0 - 0.5 * (max_hold - 6.0)
    elif max_hold <= 8.5:
        hold_score = 8.5 - ((max_hold - 7.0) / 1.5)
    else:
        hold_score = 6.0
    density_score = 10.0 if shot_count >= 3 or semantic_beats <= 2 else 9.0 if shot_count >= 2 else 7.0
    score = _bounded(0.75 * hold_score + 0.25 * density_score)
    return score, {
        "release_profile": profile,
        "format_class": "short",
        "max_shot_hold_seconds": round(max_hold, 3),
        "shot_count": shot_count,
        "semantic_beat_count": semantic_beats,
        "hold_score_10": round(hold_score, 3),
        "density_score_10": round(density_score, 3),
        "score_10": round(score, 3),
        "minimum_required_score_10": MIN_SHORT_PACING_SCORE,
        "pass": score >= MIN_SHORT_PACING_SCORE,
    }


def _p4_binding_summary(p4: dict[str, Any], *, profile: str) -> dict[str, Any]:
    acceptance = p4.get("acceptance_contract") if isinstance(p4, dict) else None
    if not isinstance(acceptance, dict):
        raise RuntimeError("Viewer Quality Contract requires Final Master acceptance contract")
    sources = acceptance.get("sources")
    if not isinstance(sources, dict):
        raise RuntimeError("Viewer Quality Contract requires Final Master source bindings")
    final_binding = sources.get("final")
    if not isinstance(final_binding, dict) or not str(final_binding.get("sha256") or ""):
        raise RuntimeError("Viewer Quality Contract requires Final Master final.mp4 binding")
    short_timeline = sources.get("short_visual_timeline")
    is_short = profile in {"standalone_short", "derived_short"}
    if is_short and not isinstance(short_timeline, dict):
        raise RuntimeError(
            "Viewer Quality Contract requires short-visual-timeline.json to be sealed by Final Master"
        )
    return {
        "contract_id": acceptance.get("contract_id"),
        "final_sha256": final_binding.get("sha256"),
        "final_byte_length": final_binding.get("byte_length"),
        "short_visual_timeline_required": is_short,
        "short_visual_timeline_bound": isinstance(short_timeline, dict),
        "short_visual_timeline_sha256": (
            short_timeline.get("sha256") if isinstance(short_timeline, dict) else None
        ),
        "short_visual_timeline_byte_length": (
            short_timeline.get("byte_length") if isinstance(short_timeline, dict) else None
        ),
        "validation": "exact_source_bindings_revalidated_before_and_after_viewer",
    }


def enforce_viewer_quality_contract(
    output_dir: Path,
    *,
    fmt: str,
    critic: dict[str, Any],
    gold_enforce: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate exact final rendered bytes before production state acceptance.

    ``gold_enforce`` is accepted for backward compatibility with the first branch
    implementation and post-Gold diagnostic revalidation, but it is not an authority.
    The enforcing Gold critic itself is the pre-acceptance evidence used here.

    Final Master is revalidated immediately before Viewer reads any evidence and again
    after scoring. For both Standalone and Derived Shorts the exact Short timeline must
    already be part of the same Final Master source receipt as ``final.mp4``. A stale,
    newly-created-after-QC, mutated timeline, or mutated final render therefore fails
    closed before publication state can be accepted.
    """
    del gold_enforce
    root = Path(output_dir)
    normalized_fmt = str(fmt).strip().lower()
    profile = _release_profile(root, normalized_fmt)

    p4_before = require_final_master_acceptance(root)
    p4_binding = _p4_binding_summary(p4_before, profile=profile)

    qc = _read_object(root / "final-master-qc.json")
    quality = _read_object(root / "quality-final.json")
    visual_records = _final_cut_visual_records(root)

    technical_score, technical = _technical_score(qc, quality)
    visual_score, visual = _visual_score(visual_records)
    if profile == "film":
        film_coverage = _film_visual_coverage(root, visual_records)
        visual["film_coverage_contract"] = film_coverage
        visual["pass"] = visual.get("pass") is True and film_coverage.get("pass") is True
    audio_score, audio = _audio_score(quality)
    pacing_score, pacing = _pacing_score(root, normalized_fmt, profile, qc)

    gold_pass = (
        isinstance(critic, dict)
        and str(critic.get("status") or "").lower() == "pass"
        and not list(critic.get("hard_blocks") or ())
        and str(critic.get("observation_status") or "ok").lower() != "failed_observation"
    )
    gold_score = 10.0 if gold_pass else 0.0

    weights = {
        "visual_semantics": 0.35,
        "pacing": 0.20,
        "audio_av": 0.15,
        "technical": 0.15,
        "gold": 0.15,
    }
    overall = (
        weights["visual_semantics"] * visual_score
        + weights["pacing"] * pacing_score
        + weights["audio_av"] * audio_score
        + weights["technical"] * technical_score
        + weights["gold"] * gold_score
    )
    non_compensable = {
        "gold": gold_pass,
        "technical": technical.get("pass") is True,
        "visual_semantics": visual.get("pass") is True,
        "pacing": pacing.get("pass") is True,
        "audio_av": audio.get("pass") is True,
    }
    verdict = "pass" if overall >= MIN_VIEWER_SCORE and all(non_compensable.values()) else "block"

    # Revalidate the exact same source identities after all Viewer reads. The Final
    # Master contract recomputes sha256+byte length for every current source, including
    # the Short timeline when Moment owns one.
    p4_after = require_final_master_acceptance(root)
    p4_after_binding = _p4_binding_summary(p4_after, profile=profile)
    if p4_after_binding != p4_binding:
        raise RuntimeError("Viewer Quality Contract detected Final Master binding drift")

    document = {
        "schema_version": CONTRACT_VERSION,
        "contract_id": CONTRACT_ID,
        "format": normalized_fmt,
        "release_profile": profile,
        "viewer_score_10": round(_bounded(overall), 3),
        "minimum_viewer_score_10": MIN_VIEWER_SCORE,
        "verdict": verdict,
        "acceptance_phase": "pre_state_acceptance",
        "score_meaning": "deterministic engineering release-confidence envelope; not human MOS or YouTube forecast",
        "calibration_status": "not_yet_calibrated_against_blind_human_panel",
        "final_master_binding": p4_binding,
        "weights": weights,
        "dimensions": {
            "visual_semantics": visual,
            "pacing": pacing,
            "audio_av": audio,
            "technical": technical,
            "gold": {"score_10": gold_score, "candidate_pass": gold_pass, "pass": gold_pass},
        },
        "non_compensable_gates": non_compensable,
        "provider_calls_added": 0,
    }
    (root / FILENAME).write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    if verdict != "pass":
        failed = [name for name, ok in non_compensable.items() if not ok]
        raise RuntimeError(
            "Viewer Quality Contract V1 blocked release: "
            f"score={overall:.3f} required={MIN_VIEWER_SCORE:.1f} failed={','.join(failed) or 'score'}"
        )

    # #228 is explicitly Short-only. Film and Story return not_applicable here and are
    # never compared against the Moment reference; Long needs its own future benchmark
    # set and range calibration.
    regression = enforce_run_228_viewer_regression(root, viewer_report=document)
    document["viewer_regression"] = {
        "baseline_run": 228,
        "status": regression.get("status"),
        "run_228_governs_long": False,
        "provider_calls_added": 0,
    }
    (root / FILENAME).write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "Viewer Quality Contract V1 PASS before state acceptance: "
        f"profile={profile} format={normalized_fmt} score={overall:.3f}/10 "
        f"visual={visual_score:.3f} pacing={pacing_score:.3f} audio={audio_score:.3f} "
        f"run228={regression.get('status')}"
    )
    return document
