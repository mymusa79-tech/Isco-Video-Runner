from __future__ import annotations

"""Deterministic viewer-quality release envelope for every canonical V4 format.

This contract adds no model calls.  It composes evidence that the current pipeline has
already paid to obtain: Final Master QC, audio/A-V measurements, accepted visual audits,
short cinematic pacing when applicable, and Gold acceptance.  It cannot turn a failed
upstream gate into PASS; it can only require a stronger release margin.

The score is a 0-10 engineering confidence score, not a promise of YouTube performance.
A canonical release requires >=8.5 overall plus non-compensable sub-gates, so perfect
technical encoding cannot hide weak visual semantics or poor short pacing.
"""

import json
import math
from pathlib import Path
from typing import Any, Iterable


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


def _accepted_visual_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in (
        "visual-audit.json",
        "short-cinematic-visual-audit.json",
        "short-cinematic-visual-audit.partial.json",
    ):
        path = root / name
        if not path.is_file():
            continue
        try:
            payload = _read_json(path)
        except Exception:
            continue
        items: Iterable[Any]
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("audits") or payload.get("results") or payload.get("entries") or ()
        else:
            items = ()
        for item in items:
            if not isinstance(item, dict) or str(item.get("status") or "").lower() != "pass":
                continue
            try:
                relevance = float(item.get("relevance"))
                quality = float(item.get("visual_quality"))
            except (TypeError, ValueError):
                continue
            if not (0.0 <= relevance <= 1.0 and 0.0 <= quality <= 1.0):
                continue
            records.append(
                {
                    "source": name,
                    "provider": item.get("provider"),
                    "candidate_id": item.get("candidate_id"),
                    "relevance": relevance,
                    "visual_quality": quality,
                    "semantic_floor": min(relevance, quality),
                }
            )
    return records


def _visual_score(records: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
    if not records:
        raise RuntimeError("Viewer Quality Contract requires accepted visual-audit evidence")
    semantic = [float(item["semantic_floor"]) for item in records]
    # A trimmed/upper-compensating average would let many excellent shots hide one weak
    # editorial placeholder.  Use the ordinary mean plus an explicit weakest-shot floor.
    mean = sum(semantic) / len(semantic)
    weakest = min(semantic)
    score = _bounded(10.0 * mean)
    return score, {
        "accepted_records": len(records),
        "semantic_mean": round(mean, 4),
        "semantic_min": round(weakest, 4),
        "score_10": round(score, 3),
        "minimum_required_score_10": MIN_VISUAL_SEMANTIC_SCORE,
        "weakest_final_visual_floor": 0.80,
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


def _short_pacing_score(root: Path, fmt: str, qc: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    if fmt not in {"moment", "story"}:
        freeze_events = list(((qc.get("detectors") or {}).get("exact_freeze") or {}).get("events") or ())
        score = 10.0 if not freeze_events else 8.0
        return score, {
            "format_class": "long",
            "exact_freeze_events": len(freeze_events),
            "score_10": score,
            "pass": not freeze_events,
        }

    path = root / "short-visual-timeline.json"
    timeline = _read_object(path)
    try:
        max_hold = float((timeline.get("visual_density_contract") or {}).get("actual_max_shot_hold_seconds"))
        shot_count = int(timeline.get("shot_count") or 0)
        semantic_beats = int(timeline.get("semantic_beat_count") or 0)
    except (TypeError, ValueError):
        raise RuntimeError("Viewer Quality Contract requires Short visual pacing evidence")

    # Human-facing pacing margin.  The existing hard max remains authoritative; this
    # stronger release envelope rewards purposeful cuts without forcing hyperactive edits.
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


def enforce_viewer_quality_contract(
    output_dir: Path,
    *,
    fmt: str,
    critic: dict[str, Any],
    gold_enforce: dict[str, Any],
) -> dict[str, Any]:
    root = Path(output_dir)
    qc = _read_object(root / "final-master-qc.json")
    quality = _read_object(root / "quality-final.json")
    visual_records = _accepted_visual_records(root)

    technical_score, technical = _technical_score(qc, quality)
    visual_score, visual = _visual_score(visual_records)
    audio_score, audio = _audio_score(quality)
    pacing_score, pacing = _short_pacing_score(root, str(fmt).strip().lower(), qc)

    gold = gold_enforce.get("gold") if isinstance(gold_enforce, dict) else None
    gold_pass = isinstance(gold, dict) and gold.get("accepted") is True
    if isinstance(critic, dict) and critic.get("hard_blocks"):
        gold_pass = False
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
    document = {
        "schema_version": CONTRACT_VERSION,
        "contract_id": CONTRACT_ID,
        "format": str(fmt).strip().lower(),
        "viewer_score_10": round(_bounded(overall), 3),
        "minimum_viewer_score_10": MIN_VIEWER_SCORE,
        "verdict": verdict,
        "score_meaning": "deterministic release-confidence envelope; not a forecast of YouTube performance",
        "weights": weights,
        "dimensions": {
            "visual_semantics": visual,
            "pacing": pacing,
            "audio_av": audio,
            "technical": technical,
            "gold": {"score_10": gold_score, "pass": gold_pass},
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
    print(
        "Viewer Quality Contract V1 PASS: "
        f"format={fmt} score={overall:.3f}/10 visual={visual_score:.3f} "
        f"pacing={pacing_score:.3f} audio={audio_score:.3f}"
    )
    return document
