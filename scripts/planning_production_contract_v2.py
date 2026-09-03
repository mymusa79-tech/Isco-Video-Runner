from __future__ import annotations

import functools
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged

from scripts import planning_checkpoint_state as durable_state
from scripts import planning_stage_contract as stage_contract
from scripts.runtime_phase import canonical_runtime_enabled

CONTRACT_ID = "planning.production.v2"
FAMILY_ID = "planning.production"
REPORT_FILENAME = "planning-production-contract-v2.json"
SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]

_PROVIDER_ACCEPTANCE_SECONDS = {
    "gemini": 120.0,
    "groq": 90.0,
    "openrouter": 120.0,
}
_STAGE_WALL_SECONDS = 300.0
_LONG_FAMILY_WALL_SECONDS = 1500.0
_SHORT_FAMILY_WALL_SECONDS = 720.0
_TERMINAL_RESET_MAX_SECONDS = 60.0

_INSTALLED = False
_STAGE_RECEIPTS: list[dict[str, Any]] = []
_FAMILY_STARTED_AT: float | None = None


class PlanningFamilyErrorCode(str, Enum):
    AUTH_CONFIG = "AUTH_CONFIG"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    FINAL_PLAN_INVALID = "FINAL_PLAN_INVALID"
    LINEAGE_INVALID = "LINEAGE_INVALID"


@dataclass(frozen=True)
class DeadlinePolicy:
    provider_acceptance_seconds: tuple[tuple[str, float], ...]
    max_stage_wall_seconds: float
    max_retry_after_seconds: float
    max_terminal_reset_wait_seconds: float
    long_family_wall_seconds: float
    short_family_wall_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_acceptance_seconds": dict(self.provider_acceptance_seconds),
            "max_stage_wall_seconds": self.max_stage_wall_seconds,
            "max_retry_after_seconds": self.max_retry_after_seconds,
            "max_terminal_reset_wait_seconds": self.max_terminal_reset_wait_seconds,
            "long_family_wall_seconds": self.long_family_wall_seconds,
            "short_family_wall_seconds": self.short_family_wall_seconds,
        }


def deadline_policy() -> DeadlinePolicy:
    retry_after = min(
        20.0,
        float(getattr(stage_contract.router, "RETRY_AFTER_MAX_SECONDS", 20.0) or 20.0),
    )
    return DeadlinePolicy(
        provider_acceptance_seconds=tuple(sorted(_PROVIDER_ACCEPTANCE_SECONDS.items())),
        max_stage_wall_seconds=_STAGE_WALL_SECONDS,
        max_retry_after_seconds=retry_after,
        max_terminal_reset_wait_seconds=_TERMINAL_RESET_MAX_SECONDS,
        long_family_wall_seconds=_LONG_FAMILY_WALL_SECONDS,
        short_family_wall_seconds=_SHORT_FAMILY_WALL_SECONDS,
    )


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            f"invalid_json:{Path(path).name}:{type(exc).__name__}",
        ) from exc
    if not isinstance(value, dict):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            f"wrong_shape:{Path(path).name}",
        )
    return value


def _semantic_plan_payload(plan: dict[str, Any]) -> dict[str, Any]:
    value = dict(plan)
    value.pop("plan_source", None)
    return value


def _semantic_plan_sha256(plan: dict[str, Any]) -> str:
    return _sha256_bytes(_canonical_json(_semantic_plan_payload(plan)))


def reset_runtime_evidence_for_tests() -> None:
    global _FAMILY_STARTED_AT
    _STAGE_RECEIPTS.clear()
    _FAMILY_STARTED_AT = None


def _begin_family_if_needed() -> None:
    global _FAMILY_STARTED_AT
    if _FAMILY_STARTED_AT is None:
        _FAMILY_STARTED_AT = time.monotonic()


def _deadline_contract_rules() -> dict[str, Any]:
    return {"deadline_policy": deadline_policy().as_dict()}


def _record_stage_receipt(
    contract: stage_contract.PlanningStageContract,
    model: str,
    payload: dict,
    *,
    provider: str,
    cache_hit: bool,
) -> None:
    receipt = {
        "sequence": len(_STAGE_RECEIPTS) + 1,
        "stage_id": contract.stage_id,
        "contract_id": contract.contract_id,
        "input_hash": contract.input_hash,
        "contract_fingerprint": stage_contract._contract_fingerprint(contract),
        "requested_model": str(model),
        "accepted_provider": str(provider),
        "cache_hit": bool(cache_hit),
        "cache_revalidated": bool(cache_hit and contract.cache_policy.revalidate_on_hit),
        "output_sha256": _sha256_bytes(_canonical_json(payload)),
        "deadline_policy": contract.semantic_rules.get("deadline_policy"),
    }
    _STAGE_RECEIPTS.append(receipt)


def _approved_input_identity() -> dict[str, Any]:
    raw = str(os.environ.get("ISCO_APPROVED_BRIEF_PATH") or "").strip()
    if not raw:
        if canonical_runtime_enabled():
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "canonical runtime has no approved brief path",
            )
        return {"path": None, "sha256": None}
    path = Path(raw)
    if not path.is_file():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved brief path is missing",
        )
    return {"path": path.name, "sha256": _sha256_file(path)}


def _research_identity(root: Path) -> dict[str, Any]:
    path = root / "research-provenance.json"
    if not path.is_file():
        if canonical_runtime_enabled():
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "research-provenance.json missing before Planning handoff",
            )
        return {"file": None, "sha256": None}
    return {"file": path.name, "sha256": _sha256_file(path)}


def _runtime_contract_sha256() -> str:
    return durable_state.planning_contract_sha256(ROOT)


def _family_limit_for_format(fmt: str) -> float:
    return (
        deadline_policy().short_family_wall_seconds
        if str(fmt).strip().lower() == "moment"
        else deadline_policy().long_family_wall_seconds
    )


def _require_family_wall_budget(fmt: str) -> float:
    if _FAMILY_STARTED_AT is None:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "no Planning stage evidence was recorded",
        )
    elapsed = max(0.0, time.monotonic() - _FAMILY_STARTED_AT)
    limit = _family_limit_for_format(fmt)
    if elapsed > limit:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.DEADLINE_EXCEEDED,
            f"planning_family_wall_seconds={elapsed:.3f} limit={limit:.3f}",
        )
    return elapsed


def certify_planning_handoff(output_dir: Path, plan_object: object | None = None) -> dict[str, Any]:
    root = Path(output_dir)
    plan_path = root / "plan.json"
    if not plan_path.is_file():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan.json missing before P2/P3 handoff",
        )
    plan = _load_json(plan_path)
    fmt = str(plan.get("format") or getattr(plan_object, "format", "") or "").strip().lower()
    topic = str(plan.get("topic") or getattr(plan_object, "topic", "") or "").strip()
    if fmt not in {"film", "story", "moment"} or not topic:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            f"final plan identity invalid format={fmt or 'missing'} topic_present={bool(topic)}",
        )
    if not _STAGE_RECEIPTS:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "final plan has no accepted Planning Stage receipts",
        )

    elapsed = _require_family_wall_budget(fmt)
    document = {
        "schema_version": SCHEMA_VERSION,
        "family_id": FAMILY_ID,
        "contract_id": CONTRACT_ID,
        "decision": "pass",
        "format": fmt,
        "topic_sha256": _sha256_bytes(topic.encode("utf-8")),
        "approved_input": _approved_input_identity(),
        "research_context": _research_identity(root),
        "planning_runtime_contract_sha256": _runtime_contract_sha256(),
        "runner_sha": str(os.environ.get("GITHUB_SHA") or "").strip() or None,
        "engine_sha": str(os.environ.get("ISCO_ENGINE_SHA") or "").strip() or None,
        "deadline_policy": deadline_policy().as_dict(),
        "planning_wall_seconds": round(elapsed, 3),
        "stage_receipts": list(_STAGE_RECEIPTS),
        "stage_receipt_count": len(_STAGE_RECEIPTS),
        "final_plan_file_sha256": _sha256_file(plan_path),
        "final_plan_semantic_sha256": _semantic_plan_sha256(plan),
        "annotations": {},
        "handoff": {
            "next": ["director_phase_a", "tts_or_short_voice", "visual_production"],
            "requires_exact_plan_semantics": True,
        },
    }
    (root / REPORT_FILENAME).write_bytes(_canonical_json(document))
    require_planning_handoff(root)
    print(
        "Planning Production Contract V2 PASS: "
        f"format={fmt} stages={len(_STAGE_RECEIPTS)} wall={elapsed:.2f}s"
    )
    return document


def require_planning_handoff(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    report_path = root / REPORT_FILENAME
    plan_path = root / "plan.json"
    if not report_path.is_file() or not plan_path.is_file():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning Production Contract V2 certificate or plan.json missing",
        )
    report = _load_json(report_path)
    plan = _load_json(plan_path)
    if report.get("decision") != "pass" or report.get("contract_id") != CONTRACT_ID:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning Production Contract V2 certificate is not authoritative",
        )
    if report.get("final_plan_file_sha256") != _sha256_file(plan_path):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan.json bytes changed after Planning certification",
        )
    if report.get("final_plan_semantic_sha256") != _semantic_plan_sha256(plan):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan.json semantics changed after Planning certification",
        )
    approved = report.get("approved_input")
    current_approved = _approved_input_identity()
    if not isinstance(approved, dict) or approved.get("sha256") != current_approved.get("sha256"):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved Planning input changed after certification",
        )
    research = report.get("research_context")
    current_research = _research_identity(root)
    if not isinstance(research, dict) or research.get("sha256") != current_research.get("sha256"):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning research context changed after certification",
        )
    if report.get("planning_runtime_contract_sha256") != _runtime_contract_sha256():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning runtime contract changed after certification",
        )
    return report


def _rebind_plan_source_annotation(output_dir: Path, source: str) -> None:
    root = Path(output_dir)
    report = require_planning_handoff(root)
    plan_path = root / "plan.json"
    plan = _load_json(plan_path)
    before_semantic = _semantic_plan_sha256(plan)
    plan["plan_source"] = str(source)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    after = _load_json(plan_path)
    if _semantic_plan_sha256(after) != before_semantic:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan_source annotation changed certified plan semantics",
        )
    report["final_plan_file_sha256"] = _sha256_file(plan_path)
    report["final_plan_semantic_sha256"] = before_semantic
    annotations = report.setdefault("annotations", {})
    annotations["plan_source"] = str(source)
    (root / REPORT_FILENAME).write_bytes(_canonical_json(report))
    require_planning_handoff(root)


def _install_stage_evidence_hooks() -> None:
    original_bind = stage_contract.bind_request_contract
    if not getattr(original_bind, "_isco_planning_production_v2", False):
        @functools.wraps(original_bind)
        def bind_request_contract(spec, effective_prompt):
            _begin_family_if_needed()
            contract = original_bind(spec, effective_prompt)
            rules = dict(contract.semantic_rules)
            rules.update(_deadline_contract_rules())
            return replace(contract, semantic_rules=rules)

        bind_request_contract._isco_planning_production_v2 = True
        bind_request_contract._isco_original = original_bind
        stage_contract.bind_request_contract = bind_request_contract

    original_read = stage_contract._cache_read
    if not getattr(original_read, "_isco_planning_production_v2", False):
        @functools.wraps(original_read)
        def cache_read(checkpoint, contract, model):
            payload = original_read(checkpoint, contract, model)
            if isinstance(payload, dict):
                _record_stage_receipt(
                    contract,
                    model,
                    payload,
                    provider="cache",
                    cache_hit=True,
                )
            return payload

        cache_read._isco_planning_production_v2 = True
        cache_read._isco_original = original_read
        stage_contract._cache_read = cache_read

    original_commit = stage_contract._cache_commit
    if not getattr(original_commit, "_isco_planning_production_v2", False):
        @functools.wraps(original_commit)
        def cache_commit(checkpoint, contract, model, payload, provider):
            original_commit(checkpoint, contract, model, payload, provider)
            _record_stage_receipt(
                contract,
                model,
                payload,
                provider=provider,
                cache_hit=False,
            )

        cache_commit._isco_planning_production_v2 = True
        cache_commit._isco_original = original_commit
        stage_contract._cache_commit = cache_commit


def _install_error_taxonomy_hook() -> None:
    original = stage_contract._provider_failure
    if getattr(original, "_isco_planning_production_v2", False):
        return

    @functools.wraps(original)
    def provider_failure(contract, provider, exc):
        classified, retryable, retry_after, failure = original(contract, provider, exc)
        if getattr(failure, "telemetry_result", "") in {
            "auth_error",
            "bad_request",
            "model_not_found",
        }:
            classified = stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.AUTH_CONFIG,
                str(exc).replace("\n", " ")[:300],
                stage_id=contract.stage_id,
                provider=provider,
            )
            retryable = False
        return classified, retryable, retry_after, failure

    provider_failure._isco_planning_production_v2 = True
    provider_failure._isco_original = original
    stage_contract._provider_failure = provider_failure


def _install_stage_deadline_hook() -> None:
    current = staged.json_text
    if getattr(current, "_isco_planning_production_deadline_v2", False):
        return

    @functools.wraps(current)
    def bounded_json_text(*args, **kwargs):
        started = time.monotonic()
        result = current(*args, **kwargs)
        elapsed = time.monotonic() - started
        if elapsed > deadline_policy().max_stage_wall_seconds:
            spec = stage_contract._ACTIVE_STAGE_SPEC.get()
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.DEADLINE_EXCEEDED,
                f"planning_stage_wall_seconds={elapsed:.3f} "
                f"limit={deadline_policy().max_stage_wall_seconds:.3f}",
                stage_id=getattr(spec, "stage_id", None),
            )
        return result

    bounded_json_text._isco_planning_production_deadline_v2 = True
    bounded_json_text._isco_original = current
    setattr(bounded_json_text, stage_contract._ROUTER_MARKER, True)
    staged.json_text = bounded_json_text


def _install_handoff_gate() -> None:
    current = orchestrator._observe_director_phase_a
    if getattr(current, "_isco_planning_production_handoff_v2", False):
        return

    @functools.wraps(current)
    def certified_observe_director_phase_a(*args, **kwargs):
        output_dir = kwargs.get("out")
        plan = kwargs.get("plan")
        if output_dir is None:
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "Director Phase A called without output directory",
            )
        certify_planning_handoff(Path(output_dir), plan)
        require_planning_handoff(Path(output_dir))
        return current(*args, **kwargs)

    certified_observe_director_phase_a._isco_planning_production_handoff_v2 = True
    certified_observe_director_phase_a._isco_original = current
    orchestrator._observe_director_phase_a = certified_observe_director_phase_a


def _install_plan_source_rebind() -> None:
    candidates = [
        sys.modules.get("scripts.run_v3_voice"),
        sys.modules.get("__main__"),
    ]
    for module in candidates:
        if module is None:
            continue
        current = getattr(module, "_tag_plan_source", None)
        if not callable(current) or getattr(current, "_isco_planning_production_v2", False):
            continue

        @functools.wraps(current)
        def tagged(out_dir, _current=current, _module=module):
            source_resolver = getattr(_module, "_resolve_plan_source", None)
            if not callable(source_resolver):
                return _current(out_dir)
            source = source_resolver()
            _rebind_plan_source_annotation(Path(out_dir), source)
            quality = Path(out_dir) / "quality-final.json"
            if quality.is_file():
                data = _load_json(quality)
                data["plan_source"] = source
                quality.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(f"Plan source tagged under Planning Production Contract V2: {source}")

        tagged._isco_planning_production_v2 = True
        tagged._isco_original = current
        setattr(module, "_tag_plan_source", tagged)


def install_planning_production_contract_v2() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    reset_runtime_evidence_for_tests()
    _install_stage_evidence_hooks()
    _install_error_taxonomy_hook()
    _install_stage_deadline_hook()
    _install_handoff_gate()
    _install_plan_source_rebind()
    _INSTALLED = True
    print(
        "Planning Production Contract V2 installed: "
        "Long+Short stage lineage + exact plan handoff + AUTH_CONFIG taxonomy + "
        "deadline fingerprint + fail-closed P2/P3 boundary"
    )
