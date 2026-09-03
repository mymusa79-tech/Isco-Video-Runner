from __future__ import annotations

"""F23: one Long + Standalone-Short Planning production contract.

The existing Planning Stage Contract remains the sole schema/provider/cache owner. F23
adds family-level evidence and the final fail-closed handoff to P2/P3.
"""

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
from isco_video_agent.security import normalize_topic

from scripts import planning_checkpoint_state as durable_state
from scripts import planning_stage_contract as stage_contract
from scripts.product_proof_plan import was_fallback_used
from scripts.runtime_phase import canonical_runtime_enabled


CONTRACT_ID = "planning.production.v2"
FAMILY_ID = "planning.production"
REPORT_FILENAME = "planning-production-contract-v2.json"
SCHEMA_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]

# F23 records these transport/deadline expectations in contract identity. Existing
# Run123/124/128 owners still perform Retry-After and terminal-reset pacing.
_PROVIDER_ACCEPTANCE_SECONDS = {
    "gemini": 120.0,
    "groq": 90.0,
    "openrouter": 120.0,
}
_STAGE_WALL_SECONDS = 300.0
_LONG_FAMILY_WALL_SECONDS = 1500.0
_SHORT_FAMILY_WALL_SECONDS = 720.0
_RETRY_AFTER_ACCEPTANCE_SECONDS = 20.0
_TERMINAL_RESET_MAX_SECONDS = 60.0

_PLANNING_GATE_ARTIFACTS = (
    "repair-dossier.json",
    "factuality-audit.json",
    "content-quality-audit.json",
    "tone-quality-audit.json",
    "quality-precheck.json",
)

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
    return DeadlinePolicy(
        provider_acceptance_seconds=tuple(sorted(_PROVIDER_ACCEPTANCE_SECONDS.items())),
        max_stage_wall_seconds=_STAGE_WALL_SECONDS,
        max_retry_after_seconds=_RETRY_AFTER_ACCEPTANCE_SECONDS,
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


def _valid_commit_sha(value: object) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


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
    # run_v3_voice adds this diagnostic annotation only after production returns.
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
    _STAGE_RECEIPTS.append(
        {
            "sequence": len(_STAGE_RECEIPTS) + 1,
            "lineage_kind": "accepted_stage_response",
            "stage_id": contract.stage_id,
            "contract_id": contract.contract_id,
            "input_hash": contract.input_hash,
            "contract_fingerprint": stage_contract._contract_fingerprint(contract),
            "requested_model": str(model),
            "accepted_provider": str(provider),
            "cache_hit": bool(cache_hit),
            "cache_revalidated": bool(
                cache_hit and contract.cache_policy.revalidate_on_hit
            ),
            "output_sha256": _sha256_bytes(_canonical_json(payload)),
            "deadline_policy": contract.semantic_rules.get("deadline_policy"),
        }
    )


def _approved_input_identity() -> dict[str, Any]:
    raw = str(os.environ.get("ISCO_APPROVED_BRIEF_PATH") or "").strip()
    if not raw:
        if canonical_runtime_enabled():
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "canonical runtime has no approved brief path",
            )
        return {
            "path": None,
            "sha256": None,
            "topic_sha256": None,
            "format": None,
            "approved_by_user": None,
        }

    path = Path(raw)
    if not path.is_file() or path.is_symlink():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved brief path is missing or not a regular file",
        )
    try:
        brief = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            f"approved brief is invalid JSON:{type(exc).__name__}",
        ) from exc
    if not isinstance(brief, dict):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved brief is not an object",
        )

    approved_topic = normalize_topic(str(brief.get("approved_topic") or ""))
    approved_format = str(brief.get("format") or "").strip().lower()
    approved = brief.get("approved_by_user") is True
    if not approved or not approved_topic or approved_format not in {
        "film",
        "story",
        "moment",
    }:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved brief identity/approval is invalid",
        )
    return {
        "path": path.name,
        "sha256": _sha256_file(path),
        "topic_sha256": _sha256_bytes(approved_topic.encode("utf-8")),
        "format": approved_format,
        "approved_by_user": True,
    }


def _research_identity(root: Path) -> dict[str, Any]:
    path = root / "research-provenance.json"
    if not path.is_file() or path.is_symlink():
        if canonical_runtime_enabled():
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "research-provenance.json missing before Planning handoff",
            )
        return {"file": None, "sha256": None}
    return {"file": path.name, "sha256": _sha256_file(path)}


def _planning_gate_evidence(root: Path) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for filename in _PLANNING_GATE_ARTIFACTS:
        path = root / filename
        if not path.is_file() or path.is_symlink():
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                f"planning gate evidence missing before handoff:{filename}",
            )
        _load_json(path)
        evidence[filename] = _sha256_file(path)
    return evidence


def _runtime_contract_sha256() -> str:
    return durable_state.planning_contract_sha256(ROOT)


def _family_limit_for_format(fmt: str) -> float:
    return (
        deadline_policy().short_family_wall_seconds
        if str(fmt).strip().lower() == "moment"
        else deadline_policy().long_family_wall_seconds
    )


def _require_family_wall_budget(fmt: str, *, allow_unstarted: bool = False) -> float:
    if _FAMILY_STARTED_AT is None:
        if allow_unstarted:
            return 0.0
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


def _fallback_receipt(
    plan: dict[str, Any],
    approved_input: dict[str, Any],
) -> dict[str, Any]:
    identity = {
        "contract_id": "planning.product_proof_fallback.v1",
        "approved_input_sha256": approved_input.get("sha256"),
        "deadline_policy": deadline_policy().as_dict(),
    }
    return {
        "sequence": 1,
        "lineage_kind": "explicit_local_product_proof_fallback",
        "stage_id": "planning.product_proof_fallback",
        "contract_id": "planning.product_proof_fallback.v1",
        "input_hash": str(approved_input.get("sha256") or ""),
        "contract_fingerprint": _sha256_bytes(_canonical_json(identity)),
        "requested_model": "local_static_product_proof",
        "accepted_provider": "local_product_proof",
        "cache_hit": False,
        "cache_revalidated": False,
        "output_sha256": _semantic_plan_sha256(plan),
        "deadline_policy": deadline_policy().as_dict(),
    }


def certify_planning_handoff(
    output_dir: Path,
    plan_object: object | None = None,
) -> dict[str, Any]:
    root = Path(output_dir)
    plan_path = root / "plan.json"
    if not plan_path.is_file() or plan_path.is_symlink():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan.json missing or not regular before P2/P3 handoff",
        )
    plan = _load_json(plan_path)
    fmt = str(
        plan.get("format") or getattr(plan_object, "format", "") or ""
    ).strip().lower()
    topic = normalize_topic(
        str(plan.get("topic") or getattr(plan_object, "topic", "") or "")
    )
    if fmt not in {"film", "story", "moment"} or not topic:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            f"final plan identity invalid format={fmt or 'missing'} topic_present={bool(topic)}",
        )
    if plan_object is not None:
        object_fmt = str(getattr(plan_object, "format", "") or "").strip().lower()
        object_topic = normalize_topic(str(getattr(plan_object, "topic", "") or ""))
        if object_fmt and object_fmt != fmt:
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
                "plan.json format differs from in-memory final plan",
            )
        if object_topic and object_topic != topic:
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
                "plan.json topic differs from in-memory final plan",
            )

    approved_input = _approved_input_identity()
    topic_sha = _sha256_bytes(topic.encode("utf-8"))
    if (
        approved_input.get("topic_sha256") not in {None, topic_sha}
        or approved_input.get("format") not in {None, fmt}
        or approved_input.get("approved_by_user") is False
    ):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "final plan topic/format is not bound to the approved brief",
        )

    fallback = bool(was_fallback_used())
    if not _STAGE_RECEIPTS and not fallback:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "final plan has no accepted Planning Stage receipts",
        )
    elapsed = _require_family_wall_budget(fmt, allow_unstarted=fallback)

    accepted_receipts = (
        [_fallback_receipt(plan, approved_input)]
        if fallback
        else list(_STAGE_RECEIPTS)
    )
    discarded_receipts = list(_STAGE_RECEIPTS) if fallback else []

    runner_sha = str(os.environ.get("GITHUB_SHA") or "").strip().lower() or None
    engine_sha = str(os.environ.get("ISCO_ENGINE_SHA") or "").strip().lower() or None
    if canonical_runtime_enabled() and (
        not _valid_commit_sha(runner_sha) or not _valid_commit_sha(engine_sha)
    ):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "canonical Planning handoff lacks exact Runner/Engine SHA",
        )

    document = {
        "schema_version": SCHEMA_VERSION,
        "family_id": FAMILY_ID,
        "contract_id": CONTRACT_ID,
        "decision": "pass",
        "mode": "product_proof_fallback" if fallback else "stage_contract",
        "source_authority": (
            "scripts.product_proof_plan._proof_plan"
            if fallback
            else "explicit_planning_stage_contract"
        ),
        "format": fmt,
        "topic_sha256": topic_sha,
        "approved_input": approved_input,
        "research_context": _research_identity(root),
        "planning_gate_evidence": _planning_gate_evidence(root),
        "planning_runtime_contract_sha256": _runtime_contract_sha256(),
        "runner_sha": runner_sha,
        "engine_sha": engine_sha,
        "deadline_policy": deadline_policy().as_dict(),
        "planning_wall_seconds": round(elapsed, 3),
        "stage_receipts": accepted_receipts,
        "stage_receipt_count": len(accepted_receipts),
        "discarded_pre_fallback_stage_receipts": discarded_receipts,
        "final_plan_file_sha256": _sha256_file(plan_path),
        "final_plan_semantic_sha256": _semantic_plan_sha256(plan),
        "annotations": {},
        "handoff": {
            "next": ["director_phase_a", "tts_or_short_voice", "visual_production"],
            "requires_exact_plan_semantics": True,
            "post_director_revalidation_required": True,
        },
    }
    (root / REPORT_FILENAME).write_bytes(_canonical_json(document))
    require_planning_handoff(root)
    print(
        "Planning Production Contract V2 PASS: "
        f"format={fmt} mode={document['mode']} stages={len(accepted_receipts)} "
        f"wall={elapsed:.2f}s"
    )
    return document


def require_planning_handoff(output_dir: Path) -> dict[str, Any]:
    root = Path(output_dir)
    report_path = root / REPORT_FILENAME
    plan_path = root / "plan.json"
    if (
        not report_path.is_file()
        or report_path.is_symlink()
        or not plan_path.is_file()
        or plan_path.is_symlink()
    ):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning Production Contract V2 certificate or plan.json missing/not regular",
        )
    report = _load_json(report_path)
    plan = _load_json(plan_path)
    if (
        report.get("decision") != "pass"
        or report.get("contract_id") != CONTRACT_ID
        or report.get("family_id") != FAMILY_ID
        or report.get("mode") not in {"stage_contract", "product_proof_fallback"}
    ):
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

    current_approved = _approved_input_identity()
    if report.get("approved_input") != current_approved:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "approved Planning input changed after certification",
        )
    current_topic = normalize_topic(str(plan.get("topic") or ""))
    if (
        report.get("topic_sha256")
        != _sha256_bytes(current_topic.encode("utf-8"))
        or current_approved.get("topic_sha256")
        not in {None, report.get("topic_sha256")}
        or current_approved.get("format")
        not in {None, str(plan.get("format") or "").strip().lower()}
    ):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "certified plan no longer matches approved topic/format",
        )

    research = report.get("research_context")
    current_research = _research_identity(root)
    if (
        not isinstance(research, dict)
        or research.get("sha256") != current_research.get("sha256")
    ):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning research context changed after certification",
        )
    if report.get("planning_gate_evidence") != _planning_gate_evidence(root):
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning gate evidence changed after certification",
        )
    if report.get("planning_runtime_contract_sha256") != _runtime_contract_sha256():
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning runtime contract changed after certification",
        )

    receipts = report.get("stage_receipts")
    if not isinstance(receipts, list) or not receipts:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.LINEAGE_INVALID,
            "Planning certificate has no final source lineage",
        )
    if report.get("mode") == "product_proof_fallback":
        first = receipts[0] if isinstance(receipts[0], dict) else {}
        if (
            report.get("source_authority") != "scripts.product_proof_plan._proof_plan"
            or first.get("stage_id") != "planning.product_proof_fallback"
        ):
            raise stage_contract.PlanningStageError(
                PlanningFamilyErrorCode.LINEAGE_INVALID,
                "product-proof fallback certificate has false source lineage",
            )
    return report


def _rebind_plan_source_annotation(output_dir: Path, source: str) -> None:
    root = Path(output_dir)
    report = require_planning_handoff(root)
    plan_path = root / "plan.json"
    plan = _load_json(plan_path)
    before_semantic = _semantic_plan_sha256(plan)
    plan["plan_source"] = str(source)
    plan_path.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    after = _load_json(plan_path)
    if _semantic_plan_sha256(after) != before_semantic:
        raise stage_contract.PlanningStageError(
            PlanningFamilyErrorCode.FINAL_PLAN_INVALID,
            "plan_source annotation changed certified plan semantics",
        )
    report["final_plan_file_sha256"] = _sha256_file(plan_path)
    report["final_plan_semantic_sha256"] = before_semantic
    report.setdefault("annotations", {})["plan_source"] = str(source)
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
            # The canonical owner still performs validation/write. F23 only observes
            # accepted output after that authority returns successfully.
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
        classified, retryable, retry_after, failure = original(
            contract, provider, exc
        )
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
        root = Path(output_dir)
        certify_planning_handoff(root, plan)
        require_planning_handoff(root)
        result = current(*args, **kwargs)
        # The Director is observe-only; prove it did not mutate the authoritative plan
        # before TTS/Short Voice or visuals can start.
        require_planning_handoff(root)
        return result

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
        if (
            not callable(current)
            or getattr(current, "_isco_planning_production_v2", False)
        ):
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
            print(
                f"Plan source tagged under Planning Production Contract V2: {source}"
            )

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
        "Long+Short exact source lineage + approved-input binding + "
        "AUTH_CONFIG taxonomy + deadline fingerprint + "
        "pre/post-Director fail-closed P2/P3 boundary"
    )
