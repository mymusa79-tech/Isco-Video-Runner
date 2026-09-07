from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
REPORT_FILENAME = "short-retention-contract.json"
TIMING_TOLERANCE_SECONDS = 0.10
END_TIMING_TOLERANCE_SECONDS = 0.20


class ShortRetentionContractError(RuntimeError):
    pass


_PLATFORM_ACTION_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "subscribe": (
        re.compile(r"\bاشترك(?:وا)?\b", re.IGNORECASE),
        re.compile(r"\bاشتراك\b", re.IGNORECASE),
        re.compile(r"\bsubscribe\b", re.IGNORECASE),
    ),
    "follow": (
        re.compile(r"\bتابع(?:ني|نا)\b", re.IGNORECASE),
        re.compile(r"\bتابع\s+(?:القناة|الحساب|للمزيد)\b", re.IGNORECASE),
        re.compile(r"\bfollow\s+(?:me|us|the\s+channel|for\s+more)\b", re.IGNORECASE),
    ),
    "comment": (
        re.compile(r"\bعل[ّ]?ق\b", re.IGNORECASE),
        re.compile(r"\b(?:اكتب|اكتبي|اكتبوا)\b.{0,28}\b(?:تعليق|التعليقات)\b", re.IGNORECASE),
        re.compile(r"\bcomment\b", re.IGNORECASE),
    ),
    "share": (
        re.compile(r"\bشارك(?:ي|وا)?\b.{0,24}\b(?:الفيديو|المقطع|هذا|هذه)\b", re.IGNORECASE),
        re.compile(r"\bshare\b", re.IGNORECASE),
    ),
    "like": (
        re.compile(r"\bلايك\b", re.IGNORECASE),
        re.compile(r"\b(?:إعجاب|اعجاب)\b", re.IGNORECASE),
        re.compile(r"\blike\b", re.IGNORECASE),
    ),
}


def _clean(value: object) -> str:
    return " ".join(str(value or "").strip().split())


def _platform_actions(text: object) -> list[str]:
    cleaned = _clean(text)
    if not cleaned:
        return []
    return sorted(
        action
        for action, patterns in _PLATFORM_ACTION_PATTERNS.items()
        if any(pattern.search(cleaned) for pattern in patterns)
    )


def _events(pre_gold: dict[str, Any]) -> list[dict[str, Any]]:
    events = [
        dict(item)
        for item in list(pre_gold.get("timed_text_events") or [])
        if isinstance(item, dict)
    ]
    if len(events) < 2:
        raise ShortRetentionContractError("short_retention_requires_two_semantic_beats")
    if _clean(events[0].get("role")) != "hook":
        raise ShortRetentionContractError("short_retention_first_beat_not_hook")
    if _clean(events[-1].get("role")) != "payoff":
        raise ShortRetentionContractError("short_retention_last_beat_not_payoff")
    for item in events:
        try:
            start = float(item.get("start") or 0.0)
            end = float(item.get("end") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ShortRetentionContractError("short_retention_invalid_beat_timing") from exc
        if end <= start:
            raise ShortRetentionContractError("short_retention_non_positive_beat_timing")
        if not _clean(item.get("text")):
            raise ShortRetentionContractError("short_retention_empty_visible_beat")
    return events


def _cta_contract(pre_gold: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    topic = pre_gold.get("topic_admission")
    if not isinstance(topic, dict) or topic.get("decision") != "pass":
        raise ShortRetentionContractError("short_retention_topic_admission_missing")
    single_action = _clean(topic.get("single_action_contract"))
    if not single_action:
        raise ShortRetentionContractError("short_retention_single_action_contract_missing")

    hook_actions = _platform_actions(events[0].get("text"))
    if hook_actions:
        raise ShortRetentionContractError(
            "short_retention_platform_cta_in_hook:" + ",".join(hook_actions)
        )

    rendered_actions: set[str] = set()
    action_locations: list[dict[str, Any]] = []
    for index, event in enumerate(events, 1):
        detected = _platform_actions(event.get("text"))
        if not detected:
            continue
        rendered_actions.update(detected)
        action_locations.append(
            {
                "beat_id": f"b{index:02d}",
                "role": _clean(event.get("role")),
                "actions": detected,
            }
        )
    if len(rendered_actions) > 1:
        raise ShortRetentionContractError(
            "short_retention_bundled_platform_cta:" + ",".join(sorted(rendered_actions))
        )

    contract_actions = _platform_actions(single_action)
    if len(contract_actions) > 1:
        raise ShortRetentionContractError(
            "short_retention_bundled_single_action_contract:" + ",".join(contract_actions)
        )
    if rendered_actions and contract_actions and rendered_actions != set(contract_actions):
        raise ShortRetentionContractError(
            "short_retention_rendered_cta_disagrees_with_single_action_contract"
        )

    return {
        "status": "pass",
        "single_action_contract": single_action,
        "single_action_platform_class": contract_actions[0] if contract_actions else None,
        "rendered_platform_actions": sorted(rendered_actions),
        "rendered_platform_action_locations": action_locations,
        "platform_cta_in_hook": False,
        "bundled_platform_cta": False,
        "policy": "one_contextual_action_never_in_hook",
    }


def _float(value: object, *, label: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ShortRetentionContractError(f"short_retention_invalid_{label}") from exc


def _standalone_payoff_visual(
    pre_gold: dict[str, Any], events: list[dict[str, Any]], payoff_beat_id: str
) -> dict[str, Any]:
    timeline = pre_gold.get("short_cinematic")
    if not isinstance(timeline, dict) or timeline.get("status") != "applied":
        raise ShortRetentionContractError("short_retention_standalone_visual_timeline_missing")
    shots = [item for item in list(timeline.get("shots") or []) if isinstance(item, dict)]
    if not shots:
        raise ShortRetentionContractError("short_retention_standalone_visual_shots_missing")

    candidates = []
    for shot in shots:
        covered = {_clean(item) for item in list(shot.get("covered_beat_ids") or [])}
        if _clean(shot.get("beat_id")) == payoff_beat_id or payoff_beat_id in covered:
            candidates.append(shot)
    if len(candidates) != 1:
        raise ShortRetentionContractError("short_retention_payoff_visual_binding_not_unique")
    shot = candidates[0]
    if shot is not shots[-1]:
        raise ShortRetentionContractError("short_retention_payoff_visual_not_final_shot")

    payoff = events[-1]
    payoff_start = _float(payoff.get("start"), label="payoff_start")
    payoff_end = _float(payoff.get("end"), label="payoff_end")
    shot_start = _float(shot.get("start_seconds"), label="payoff_shot_start")
    shot_end = _float(shot.get("end_seconds"), label="payoff_shot_end")
    if abs(shot_start - payoff_start) > TIMING_TOLERANCE_SECONDS:
        raise ShortRetentionContractError("short_retention_payoff_visual_start_mismatch")
    if abs(shot_end - payoff_end) > END_TIMING_TOLERANCE_SECONDS:
        raise ShortRetentionContractError("short_retention_payoff_visual_end_mismatch")
    if not _clean(shot.get("intended_visual")):
        raise ShortRetentionContractError("short_retention_payoff_intended_visual_missing")
    if not _clean(shot.get("provider")) or shot.get("asset_id") in (None, ""):
        raise ShortRetentionContractError("short_retention_payoff_visual_provenance_missing")

    decisions = [
        item
        for item in list(timeline.get("boundary_decisions") or [])
        if isinstance(item, dict)
    ]
    if decisions:
        final_boundary = decisions[-1]
        if (
            _clean(final_boundary.get("to_beat_id")) != payoff_beat_id
            or _clean(final_boundary.get("decision")) != "CUT"
            or _clean(final_boundary.get("reason")) != "payoff_boundary"
        ):
            raise ShortRetentionContractError("short_retention_payoff_boundary_not_explicit")

    return {
        "status": "pass",
        "scope": "short_only",
        "payoff_beat_id": payoff_beat_id,
        "visual_treatment": "distinct_audited_final_shot",
        "shot_id": _clean(shot.get("shot_id")),
        "provider": _clean(shot.get("provider")),
        "asset_id": shot.get("asset_id"),
        "intended_visual": _clean(shot.get("intended_visual")),
        "start_seconds": shot_start,
        "end_seconds": shot_end,
        "semantic_visual_authority": "isco_video_agent.visual_selection + short_cinematic_cloud_visual_qa",
        "extra_ai_calls_added_by_contract": 0,
    }


def _sibling_payoff_visual(
    pre_gold: dict[str, Any], events: list[dict[str, Any]], payoff_beat_id: str
) -> dict[str, Any]:
    montage = pre_gold.get("source_safe_human_montage")
    if not isinstance(montage, dict) or montage.get("status") != "applied":
        raise ShortRetentionContractError("short_retention_source_safe_montage_missing")
    if int(montage.get("semantic_beat_count") or 0) != len(events):
        raise ShortRetentionContractError("short_retention_source_safe_beat_count_mismatch")
    decisions = [
        item
        for item in list(montage.get("boundary_decisions") or [])
        if isinstance(item, dict)
    ]
    if len(decisions) != len(events) - 1:
        raise ShortRetentionContractError("short_retention_source_safe_boundaries_incomplete")
    final_boundary = decisions[-1]
    if _clean(final_boundary.get("to_beat_id")) != payoff_beat_id:
        raise ShortRetentionContractError("short_retention_source_safe_payoff_boundary_missing")
    treatment = _clean(final_boundary.get("decision"))
    if treatment not in {"HOLD", "SUBTLE_REFRAME"}:
        raise ShortRetentionContractError("short_retention_source_safe_payoff_treatment_invalid")

    inherited = pre_gold.get("source_derived_parent_visual")
    if not isinstance(inherited, dict):
        raise ShortRetentionContractError("short_retention_parent_visual_provenance_missing")
    compensation = pre_gold.get("compensation")
    if not isinstance(compensation, dict) or compensation.get("source_parent_video_inherited") is not True:
        raise ShortRetentionContractError("short_retention_parent_visual_not_inherited")
    if int(montage.get("new_stock_assets") or 0) != 0 or int(montage.get("extra_vision_ai_calls") or 0) != 0:
        raise ShortRetentionContractError("short_retention_source_safe_visual_authority_violated")

    payoff = events[-1]
    return {
        "status": "pass",
        "scope": "short_sibling",
        "payoff_beat_id": payoff_beat_id,
        "visual_treatment": treatment,
        "boundary_reason": _clean(final_boundary.get("reason")),
        "start_seconds": _float(payoff.get("start"), label="payoff_start"),
        "end_seconds": _float(payoff.get("end"), label="payoff_end"),
        "semantic_visual_authority": "certified_parent_visual_capsule",
        "new_stock_assets": 0,
        "extra_vision_ai_calls_added_by_contract": 0,
    }


def evaluate_short_retention_contract(
    control_request: dict[str, Any], pre_gold: dict[str, Any]
) -> dict[str, Any]:
    if control_request.get("kind") != "short" or control_request.get("approved_by_user") is not True:
        raise ShortRetentionContractError("short_retention_requires_approved_short")
    scope = _clean(control_request.get("approval_scope"))
    if scope not in {"short_only", "short_sibling"}:
        raise ShortRetentionContractError("short_retention_scope_invalid")

    events = _events(pre_gold)
    cta = _cta_contract(pre_gold, events)
    payoff_beat_id = f"b{len(events):02d}"
    if scope == "short_only":
        payoff_visual = _standalone_payoff_visual(pre_gold, events, payoff_beat_id)
    else:
        payoff_visual = _sibling_payoff_visual(pre_gold, events, payoff_beat_id)

    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "short.retention.copy_visual.v1",
        "status": "pass",
        "scope": scope,
        "request_id": control_request.get("request_id"),
        "cta": cta,
        "payoff_visual": payoff_visual,
        "semantic_beat_count": len(events),
        "policy": {
            "platform_cta_in_hook": "fail_closed",
            "bundled_platform_cta": "fail_closed",
            "payoff_visual_binding": "fail_closed",
            "new_text_ai_calls": 0,
            "new_visual_ai_calls": 0,
        },
    }


def require_short_retention_contract(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
) -> dict[str, Any]:
    root = Path(output_dir)
    try:
        report = evaluate_short_retention_contract(control_request, pre_gold)
    except ShortRetentionContractError as exc:
        report = {
            "schema_version": SCHEMA_VERSION,
            "contract": "short.retention.copy_visual.v1",
            "status": "block",
            "scope": _clean(control_request.get("approval_scope")) or None,
            "request_id": control_request.get("request_id"),
            "blocking_finding": str(exc),
            "policy": {
                "platform_cta_in_hook": "fail_closed",
                "bundled_platform_cta": "fail_closed",
                "payoff_visual_binding": "fail_closed",
                "new_text_ai_calls": 0,
                "new_visual_ai_calls": 0,
            },
        }
        (root / REPORT_FILENAME).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raise
    (root / REPORT_FILENAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
