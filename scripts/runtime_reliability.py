from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_guard
import scripts.task_level_planner_router as planner_router


class FailureClass(StrEnum):
    TRANSIENT_PROVIDER = "transient_provider"
    PROVIDER_OVERLOAD = "provider_overload"
    PERMANENT_CONFIG = "permanent_config"
    SCHEMA_INVALID = "schema_invalid"
    SEMANTIC_INVALID = "semantic_invalid"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    RUNTIME_CONTRACT = "runtime_contract"
    MEDIA_FAILURE = "media_failure"
    QUALITY_BLOCK = "quality_block"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True)
class FailurePolicy:
    failure_class: FailureClass
    retryable: bool
    owner: str


_FAILURE_POLICIES: tuple[tuple[tuple[str, ...], FailurePolicy], ...] = (
    (("429", "quota", "rate limit"), FailurePolicy(FailureClass.PROVIDER_OVERLOAD, True, "provider_router")),
    (("timeout", "timed out", "connection", "network", "http 500", "http 502", "http 503", "http 504"), FailurePolicy(FailureClass.TRANSIENT_PROVIDER, True, "provider_router")),
    (("401", "403", "unauthorized", "forbidden", "authentication", "invalid api key", "bad request", "invalid argument"), FailurePolicy(FailureClass.PERMANENT_CONFIG, False, "preflight_or_provider_router")),
    (("invalid json", "complete json object", "schema", "missing id", "duplicated section id", "exact section ids"), FailurePolicy(FailureClass.SCHEMA_INVALID, True, "schema_repair")),
    (("under_section_floor", "over_section_ceiling", "over_max_append", "aggregate_overflow", "required final section band"), FailurePolicy(FailureClass.SEMANTIC_INVALID, True, "bounded_output_recovery")),
    (("ai budget authorization denied", "budget exhausted", "provider_attempt_hard_cap"), FailurePolicy(FailureClass.BUDGET_EXHAUSTED, False, "budget_ledger")),
    (("no safe/relevant candidate", "fewer than three distinct", "could not assemble enough distinct", "no downloadable"), FailurePolicy(FailureClass.RESOURCE_UNAVAILABLE, True, "visual_recovery")),
    (("runtime contract", "router is not installed", "invariant failed", "installer"), FailurePolicy(FailureClass.RUNTIME_CONTRACT, False, "runtime_preflight")),
    (("ffmpeg", "ffprobe", "media", "audio", "video", "mux", "decode", "duration contract"), FailurePolicy(FailureClass.MEDIA_FAILURE, False, "media_pipeline")),
    (("quality gate", "editorial room gate", "factuality", "tone/naturalness", "content-quality", "anti-repetition", "sensitive topic"), FailurePolicy(FailureClass.QUALITY_BLOCK, False, "quality_gate")),
)


def classify_failure(exc: BaseException) -> FailurePolicy:
    detail = f"{type(exc).__name__}: {exc}".lower()
    for markers, policy in _FAILURE_POLICIES:
        if any(marker in detail for marker in markers):
            return policy
    return FailurePolicy(FailureClass.UNEXPECTED, False, "fail_closed")


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _artifact_presence(out_dir: Path) -> list[str]:
    known = (
        "research.json",
        "plan.json",
        "repair-dossier.json",
        "quality-precheck.json",
        "ai-budget.json",
        "visual-audit.json",
        "opening-visual-audit.json",
        "narration.wav",
        "final.mp4",
        "final-master-qc.json",
        "gold-enforce-report.json",
        "production-manifest.json",
        "delivery-manifest.json",
    )
    return [name for name in known if (out_dir / name).exists()]


def write_failure_envelope(
    out_dir: Path,
    *,
    stage: str,
    exc: BaseException,
    production_id: str | None = None,
) -> Path:
    """Write stable, secret-free failure evidence without changing failure semantics."""
    policy = classify_failure(exc)
    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_id": production_id or (os.environ.get("ISCO_PRODUCTION_ID") or None),
        "stage": stage,
        "failure_class": policy.failure_class.value,
        "retryable": policy.retryable,
        "recovery_owner": policy.owner,
        "exception_type": type(exc).__name__,
        "detail": str(exc).replace("\n", " ")[:500],
        "runner_sha": (os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": (os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "github_run_id": (os.environ.get("GITHUB_RUN_ID") or "").strip() or None,
        "github_run_attempt": (os.environ.get("GITHUB_RUN_ATTEMPT") or "").strip() or None,
        "artifacts_present": _artifact_presence(out_dir),
    }
    target = out_dir / "failure-envelope.json"
    atomic_write_json(target, payload)
    return target


def _require_marker(name: str, value: object, marker: str) -> None:
    if not getattr(value, marker, False):
        raise RuntimeError(f"Runtime contract failed: {name} missing marker {marker}")


def assert_runtime_contracts() -> None:
    """Fail before provider/media work if a known-critical runtime patch chain drifted.

    These checks encode prior production incidents as permanent invariants: router
    marker preservation, deterministic-before-semantic output recovery, schema retry
    ownership, Gemini structured planning, and the opening retrieval wrapper order.
    """
    _require_marker("orchestrator.build_plan", orchestrator.build_plan, "_is_resilient_router")
    _require_marker(
        "append residual repair",
        append_guard._repair_all_residual_underlength,
        "_isco_bounded_output_recovery",
    )
    _require_marker(
        "append bounds validator",
        append_guard._validate_addition_bounds,
        "_isco_bounded_output_recovery_validator",
    )
    _require_marker(
        "full-script schema repair",
        staged._call_with_schema_repair,
        "_isco_schema_repair_policy",
    )
    _require_marker(
        "Gemini planning JSON adapter",
        planner_router.gemini_json_text,
        "_isco_gemini_planning_output_guard",
    )
    _require_marker(
        "Pexels stock search wrapper",
        orchestrator.pexels_search_videos,
        "_isco_run92_stock_pool_guard",
    )
    _require_marker(
        "Pixabay stock search wrapper",
        orchestrator.pixabay_provider.search_videos,
        "_isco_run92_stock_pool_guard",
    )
    _require_marker(
        "opening selector wrapper",
        opening_director.select_opening_sequence,
        "_isco_run92_adaptive_opening_guard",
    )
    _require_marker(
        "section selector stable-intent wrapper",
        orchestrator.select_section_sequence,
        "_isco_run92_stable_visual_intent",
    )
    _require_marker(
        "single selector stable-intent wrapper",
        orchestrator.select_with_recovery,
        "_isco_run92_stable_visual_intent",
    )
