from __future__ import annotations

"""Explicit request contracts for every Planning model call.

The planning stage is selected at a stable Python call boundary, never inferred from
prompt text.  The live router binds the selected stage to the exact effective input,
performs admission before provider contact, validates structure + semantics, and only
then commits one cache row.  Restored rows are revalidated against the same contract.
"""

import functools
import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Iterator

import isco_video_agent.repair_dossier as repair_dossier
import isco_video_agent.resilient_planner as staged
from isco_video_agent.ai_budget import TaskSpec, budget_task_scope, get_active_budget_task

from provider_failure import classify_provider_failure
from scripts import task_level_planner_router as router


class PlanningErrorCode(str, Enum):
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    CAPACITY = "CAPACITY"
    STRUCTURAL_INVALID = "STRUCTURAL_INVALID"
    SEMANTIC_INVALID = "SEMANTIC_INVALID"
    CHECKPOINT_INVALID = "CHECKPOINT_INVALID"
    INTERNAL_CONTRACT_ERROR = "INTERNAL_CONTRACT_ERROR"


class PlanningStageError(RuntimeError):
    def __init__(
        self,
        code: PlanningErrorCode,
        detail: str,
        *,
        stage_id: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.stage_id = stage_id
        self.provider = provider
        parts = [code.value]
        if stage_id:
            parts.append(f"stage={stage_id}")
        if provider:
            parts.append(f"provider={provider}")
        parts.append(detail)
        super().__init__(" ".join(parts))


@dataclass(frozen=True)
class ProviderPolicy:
    providers: tuple[str, ...]
    max_attempts_per_provider: int
    max_total_attempts: int
    completion_tokens: int
    max_prompt_utf8_bytes: tuple[tuple[str, int], ...]
    openrouter_compact_repair_max_attempts: int = 1

    def prompt_limit(self, provider: str) -> int | None:
        return dict(self.max_prompt_utf8_bytes).get(provider)


@dataclass(frozen=True)
class CachePolicy:
    read: bool = True
    write: bool = True
    revalidate_on_hit: bool = True
    evict_invalid: bool = True
    namespace: str = "planning-stage-contract-v1"


@dataclass(frozen=True)
class PlanningStageSpec:
    stage_id: str
    contract_id: str
    output_schema: dict
    semantic_rules: dict
    provider_policy: ProviderPolicy
    cache_policy: CachePolicy


@dataclass(frozen=True)
class PlanningStageContract:
    stage_id: str
    contract_id: str
    input_hash: str
    output_schema: dict
    semantic_rules: dict
    provider_policy: ProviderPolicy
    cache_policy: CachePolicy


_ACTIVE_STAGE_KIND: ContextVar[str | None] = ContextVar(
    "isco_planning_stage_kind", default=None
)
_ACTIVE_STAGE_SPEC: ContextVar[PlanningStageSpec | None] = ContextVar(
    "isco_planning_stage_spec", default=None
)
_ACTIVE_REQUEST_CONTRACT: ContextVar[PlanningStageContract | None] = ContextVar(
    "isco_planning_request_contract", default=None
)

_PROVIDER_ORDER = ("gemini", "groq", "openrouter")
_STAGE_WRAPPER_MARKER = "_isco_explicit_planning_stage_contract"
_ROUTER_MARKER = "_isco_explicit_planning_contract_router"


def _strict_object(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _string_array(*, min_items: int | None = None, max_items: int | None = None) -> dict:
    schema: dict = {"type": "array", "items": {"type": "string"}}
    if min_items is not None:
        schema["minItems"] = min_items
    if max_items is not None:
        schema["maxItems"] = max_items
    return schema


def _outline_schema(expected: int) -> dict:
    editorial_intent = _strict_object(
        {
            "editorial_thesis": {"type": "string"},
            "viewer_starting_belief": {"type": "string"},
            "hidden_assumption": {"type": "string"},
            "editorial_turn": {"type": "string"},
            "stakes": {"type": "string"},
            "viewer_promise": {"type": "string"},
            "evidence_boundaries": _string_array(min_items=1, max_items=5),
            "earned_payoff": {"type": "string"},
        },
        [
            "editorial_thesis",
            "viewer_starting_belief",
            "hidden_assumption",
            "editorial_turn",
            "stakes",
            "viewer_promise",
            "evidence_boundaries",
            "earned_payoff",
        ],
    )
    brief = _strict_object(
        {
            "id": {"type": "string"},
            "purpose": {"type": "string"},
            "visual_query": {"type": "string"},
            "on_screen_text": {"type": "string"},
            "emotion": {"type": "string"},
            "expected_seconds": {"type": "number"},
        },
        ["id", "purpose", "visual_query", "on_screen_text", "emotion", "expected_seconds"],
    )
    return _strict_object(
        {
            "pillar": {"type": "string"},
            "hook": {"type": "string"},
            "title_options": _string_array(min_items=3, max_items=3),
            "thumbnail_concepts": _string_array(min_items=3, max_items=3),
            "cta": {"type": "string"},
            "closing_payoff": {"type": "string"},
            "narrative_format": {"type": "string"},
            "opener_variant": {"type": "string"},
            "closer_variant": {"type": "string"},
            "transition_variants": _string_array(min_items=3, max_items=3),
            "editorial_intent": editorial_intent,
            "section_briefs": {
                "type": "array",
                "items": brief,
                "minItems": expected,
                "maxItems": expected,
            },
        },
        [
            "pillar",
            "hook",
            "title_options",
            "thumbnail_concepts",
            "cta",
            "closing_payoff",
            "narrative_format",
            "opener_variant",
            "closer_variant",
            "transition_variants",
            "editorial_intent",
            "section_briefs",
        ],
    )


def _script_schema(expected: int) -> dict:
    item = _strict_object(
        {
            "id": {"type": "string"},
            "narration": {"type": "string"},
            "key_point": {"type": "string"},
        },
        ["id", "narration", "key_point"],
    )
    return _strict_object(
        {
            "sections": {
                "type": "array",
                "items": item,
                "minItems": expected,
                "maxItems": expected,
            }
        },
        ["sections"],
    )


def _append_schema(expected: int) -> dict:
    item = _strict_object(
        {"id": {"type": "string"}, "append_text": {"type": "string"}},
        ["id", "append_text"],
    )
    return _strict_object(
        {
            "additions": {
                "type": "array",
                "items": item,
                "minItems": expected,
                "maxItems": expected,
            }
        },
        ["additions"],
    )


def _provider_policy(completion_tokens: int) -> ProviderPolicy:
    return ProviderPolicy(
        providers=_PROVIDER_ORDER,
        max_attempts_per_provider=router.TRANSIENT_PROVIDER_MAX_ATTEMPTS,
        max_total_attempts=router.PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS,
        completion_tokens=completion_tokens,
        # Preserve the existing evidence-backed Groq preflight boundary.  Unknown
        # provider limits are intentionally not invented here.
        max_prompt_utf8_bytes=(("groq", router.GROQ_MAX_PROMPT_UTF8_BYTES),),
        openrouter_compact_repair_max_attempts=1,
    )


def outline_stage_spec(expected_count: int) -> PlanningStageSpec:
    if expected_count <= 0:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"invalid expected_count={expected_count}",
            stage_id="planning.editorial_outline",
        )
    return PlanningStageSpec(
        stage_id="planning.editorial_outline",
        contract_id="planning.editorial_outline.v1",
        output_schema=_outline_schema(expected_count),
        semantic_rules={
            "kind": "editorial_outline",
            "expected_count": expected_count,
            "unique_nonempty_ids": True,
            "nonempty_purpose": True,
            "narrative_identity_gates": True,
        },
        provider_policy=_provider_policy(3200),
        cache_policy=CachePolicy(),
    )


def script_stage_spec(stage_kind: str, expected_ids: list[str]) -> PlanningStageSpec:
    ids = [str(item).strip() for item in expected_ids]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "script contract requires unique nonempty expected ids",
            stage_id=f"planning.{stage_kind}",
        )
    if stage_kind not in {"full_script", "script_doctor"}:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"unsupported script stage kind={stage_kind}",
        )
    return PlanningStageSpec(
        stage_id=f"planning.{stage_kind}",
        contract_id=f"planning.{stage_kind}.v1",
        output_schema=_script_schema(len(ids)),
        semantic_rules={"kind": "script", "expected_ids": ids, "exact_order": True},
        provider_policy=_provider_policy(4800),
        cache_policy=CachePolicy(),
    )


def append_stage_spec(expected_ids: list[str]) -> PlanningStageSpec:
    ids = [str(item).strip() for item in expected_ids]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "append contract requires unique nonempty expected ids",
            stage_id="planning.append_only_repair",
        )
    return PlanningStageSpec(
        stage_id="planning.append_only_repair",
        contract_id="planning.append_only_repair.v1",
        output_schema=_append_schema(len(ids)),
        semantic_rules={"kind": "append", "expected_ids": ids, "exact_order": True},
        provider_policy=_provider_policy(3200),
        cache_policy=CachePolicy(),
    )


def section_repair_stage_spec(section_id: str) -> PlanningStageSpec:
    section_id = str(section_id).strip()
    if not section_id:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "section repair contract requires section_id",
            stage_id="planning.section_repair",
        )
    return PlanningStageSpec(
        stage_id="planning.section_repair",
        contract_id="planning.section_repair.v1",
        output_schema=_strict_object({"narration": {"type": "string"}}, ["narration"]),
        semantic_rules={"kind": "section_repair", "section_id": section_id, "nonempty": True},
        provider_policy=_provider_policy(2200),
        cache_policy=CachePolicy(),
    )


def bind_request_contract(spec: PlanningStageSpec, effective_prompt: str) -> PlanningStageContract:
    input_hash = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()
    return PlanningStageContract(
        stage_id=spec.stage_id,
        contract_id=spec.contract_id,
        input_hash=input_hash,
        output_schema=spec.output_schema,
        semantic_rules=spec.semantic_rules,
        provider_policy=spec.provider_policy,
        cache_policy=spec.cache_policy,
    )


def _contract_fingerprint(contract: PlanningStageContract) -> str:
    payload = {
        "stage_id": contract.stage_id,
        "contract_id": contract.contract_id,
        "output_schema": contract.output_schema,
        "semantic_rules": contract.semantic_rules,
        "provider_policy": asdict(contract.provider_policy),
        "cache_policy": asdict(contract.cache_policy),
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cache_key(contract: PlanningStageContract, model: str) -> str:
    identity = {
        "namespace": contract.cache_policy.namespace,
        "contract_id": contract.contract_id,
        "contract_fingerprint": _contract_fingerprint(contract),
        "input_hash": contract.input_hash,
        "model": model,
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _raise_validation(code: PlanningErrorCode, contract: PlanningStageContract, path: str, detail: str) -> None:
    raise PlanningStageError(code, f"path={path} detail={detail}", stage_id=contract.stage_id)


def _validate_schema(value: object, schema: dict, contract: PlanningStageContract, path: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, dict):
            _raise_validation(PlanningErrorCode.STRUCTURAL_INVALID, contract, path, "expected_object")
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            _raise_validation(
                PlanningErrorCode.STRUCTURAL_INVALID,
                contract,
                path,
                "missing=" + ",".join(sorted(missing)),
            )
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                _raise_validation(
                    PlanningErrorCode.STRUCTURAL_INVALID,
                    contract,
                    path,
                    "unexpected=" + ",".join(extras),
                )
        for key, child_schema in properties.items():
            if key in value:
                _validate_schema(value[key], child_schema, contract, f"{path}.{key}")
        return

    if expected_type == "array":
        if not isinstance(value, list):
            _raise_validation(PlanningErrorCode.STRUCTURAL_INVALID, contract, path, "expected_array")
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            _raise_validation(
                PlanningErrorCode.STRUCTURAL_INVALID,
                contract,
                path,
                f"min_items={minimum} actual={len(value)}",
            )
        if isinstance(maximum, int) and len(value) > maximum:
            _raise_validation(
                PlanningErrorCode.STRUCTURAL_INVALID,
                contract,
                path,
                f"max_items={maximum} actual={len(value)}",
            )
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                _validate_schema(item, child, contract, f"{path}[{index}]")
        return

    if expected_type == "string":
        if not isinstance(value, str):
            _raise_validation(PlanningErrorCode.STRUCTURAL_INVALID, contract, path, "expected_string")
        return
    if expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _raise_validation(PlanningErrorCode.STRUCTURAL_INVALID, contract, path, "expected_number")


def _unique_ids(entries: list, contract: PlanningStageContract, label: str) -> list[str]:
    ids: list[str] = []
    for index, item in enumerate(entries):
        section_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if not section_id:
            _raise_validation(
                PlanningErrorCode.SEMANTIC_INVALID,
                contract,
                f"$.{label}[{index}].id",
                "empty_id",
            )
        ids.append(section_id)
    if len(ids) != len(set(ids)):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, f"$.{label}", "duplicate_ids"
        )
    return ids


def _validate_outline_semantics(data: dict, contract: PlanningStageContract) -> None:
    briefs = data.get("section_briefs")
    if not isinstance(briefs, list):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, "$.section_briefs", "expected_array"
        )
    _unique_ids(briefs, contract, "section_briefs")
    if any(not str(item.get("purpose") or "").strip() for item in briefs):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, "$.section_briefs", "empty_purpose"
        )

    narrative_format = str(data.get("narrative_format") or "").strip()
    if narrative_format not in getattr(staged, "_NARRATIVE_FORMATS", {}):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, "$.narrative_format", "unsupported"
        )
    flags = staged.validate_narrative_format(narrative_format, n=6)
    if flags:
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.narrative_format",
            "anti_repetition",
        )

    opener = str(data.get("opener_variant") or "").strip()
    closer = str(data.get("closer_variant") or "").strip()
    transitions = data.get("transition_variants")
    if not opener or not closer:
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, "$", "empty_identity_variant"
        )
    if not isinstance(transitions, list) or len(transitions) != 3 or any(
        not str(item).strip() for item in transitions
    ):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.transition_variants",
            "invalid_transitions",
        )
    identity_flags = staged.validate_identity_phrases(opener, closer, n=6)
    if identity_flags:
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID, contract, "$", "identity_anti_repetition"
        )
    try:
        staged.intent_from_dict(data.get("editorial_intent"))
    except Exception as exc:
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.editorial_intent",
            type(exc).__name__,
        )


def validate_response(contract: PlanningStageContract, data: dict) -> dict:
    _validate_schema(data, contract.output_schema, contract)
    kind = str(contract.semantic_rules.get("kind") or "")
    try:
        if kind == "editorial_outline":
            _validate_outline_semantics(data, contract)
        elif kind == "script":
            staged._parse_full_script_response(data, list(contract.semantic_rules["expected_ids"]))
        elif kind == "append":
            staged._parse_append_only_response(data, list(contract.semantic_rules["expected_ids"]))
        elif kind == "section_repair":
            if not str(data.get("narration") or "").strip():
                _raise_validation(
                    PlanningErrorCode.SEMANTIC_INVALID, contract, "$.narration", "empty"
                )
        else:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"unsupported semantic kind={kind}",
                stage_id=contract.stage_id,
            )
    except PlanningStageError:
        raise
    except Exception as exc:
        raise PlanningStageError(
            PlanningErrorCode.SEMANTIC_INVALID,
            str(exc)[:400],
            stage_id=contract.stage_id,
        ) from exc
    return data


def _load_checkpoint_strict() -> dict:
    path = router.CACHE_PATH
    if not path.exists():
        return {"version": 2, "responses": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise PlanningStageError(
            PlanningErrorCode.CHECKPOINT_INVALID,
            f"checkpoint_json_unreadable:{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("responses", {}), dict):
        raise PlanningStageError(
            PlanningErrorCode.CHECKPOINT_INVALID, "checkpoint_root_shape_invalid"
        )
    data.setdefault("version", 2)
    data.setdefault("responses", {})
    return data


def _save_checkpoint(checkpoint: dict) -> None:
    path = router.CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _evict(checkpoint: dict, key: str, error: PlanningStageError) -> None:
    responses = checkpoint["responses"]
    if key in responses:
        responses.pop(key, None)
        _save_checkpoint(checkpoint)
    print(f"Planning checkpoint evicted: {error}")


def _cache_read(
    checkpoint: dict,
    contract: PlanningStageContract,
    model: str,
) -> dict | None:
    if not contract.cache_policy.read:
        return None
    key = _cache_key(contract, model)
    row = checkpoint["responses"].get(key)
    if row is None:
        return None

    fingerprint = _contract_fingerprint(contract)
    valid_envelope = (
        isinstance(row, dict)
        and row.get("stage_id") == contract.stage_id
        and row.get("contract_id") == contract.contract_id
        and row.get("input_hash") == contract.input_hash
        and row.get("contract_fingerprint") == fingerprint
        and isinstance(row.get("payload"), dict)
    )
    if not valid_envelope:
        error = PlanningStageError(
            PlanningErrorCode.CHECKPOINT_INVALID,
            "cache_envelope_mismatch_or_legacy_row",
            stage_id=contract.stage_id,
        )
        if contract.cache_policy.evict_invalid:
            _evict(checkpoint, key, error)
        return None

    payload = row["payload"]
    if contract.cache_policy.revalidate_on_hit:
        try:
            validate_response(contract, payload)
        except PlanningStageError as exc:
            error = PlanningStageError(
                PlanningErrorCode.CHECKPOINT_INVALID,
                f"cached_payload_failed_{exc.code.value}:{exc.detail}",
                stage_id=contract.stage_id,
            )
            if contract.cache_policy.evict_invalid:
                _evict(checkpoint, key, error)
            return None
    print(f"Planning checkpoint hit: stage={contract.stage_id} contract={contract.contract_id}")
    return payload


def _cache_commit(
    checkpoint: dict,
    contract: PlanningStageContract,
    model: str,
    payload: dict,
    provider: str,
) -> None:
    """The single cache-write authority for Planning responses."""
    if not contract.cache_policy.write:
        return
    # This is intentionally redundant with provider-loop validation: this one function
    # is the only durable-write point, so it refuses any payload not authoritative now.
    validate_response(contract, payload)
    key = _cache_key(contract, model)
    checkpoint["version"] = 2
    checkpoint["last_provider"] = provider
    checkpoint["responses"][key] = {
        "stage_id": contract.stage_id,
        "contract_id": contract.contract_id,
        "input_hash": contract.input_hash,
        "contract_fingerprint": _contract_fingerprint(contract),
        "payload": payload,
    }
    _save_checkpoint(checkpoint)


def _schema_tuple(contract: PlanningStageContract) -> tuple[str, dict]:
    safe_name = contract.contract_id.replace(".", "_").replace("-", "_")[:60]
    return safe_name, contract.output_schema


def _explicit_schema_adapter(_prompt: str) -> tuple[str, dict]:
    """Compatibility seam for old low-level provider helpers; never reads prompt text."""
    contract = _ACTIVE_REQUEST_CONTRACT.get()
    if contract is None:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "legacy prompt schema inference is disabled; no active explicit request contract",
        )
    return _schema_tuple(contract)


def _admission_metadata(contract: PlanningStageContract, prompt: str) -> dict:
    expected_items = contract.semantic_rules.get("expected_count")
    if expected_items is None:
        expected_items = len(contract.semantic_rules.get("expected_ids") or []) or 1
    return {
        "stage_id": contract.stage_id,
        "contract_id": contract.contract_id,
        "input_hash": contract.input_hash,
        "prompt_utf8_bytes": len(prompt.encode("utf-8")),
        "expected_output_items": int(expected_items),
        "planned_completion_tokens": contract.provider_policy.completion_tokens,
        "max_total_attempts": contract.provider_policy.max_total_attempts,
    }


def _admit_provider(contract: PlanningStageContract, prompt: str, provider: str) -> None:
    if provider not in contract.provider_policy.providers:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"provider_not_in_policy:{provider}",
            stage_id=contract.stage_id,
            provider=provider,
        )
    prompt_bytes = len(prompt.encode("utf-8"))
    limit = contract.provider_policy.prompt_limit(provider)
    if limit is not None and prompt_bytes > limit:
        raise PlanningStageError(
            PlanningErrorCode.CAPACITY,
            f"preflight_prompt_bytes={prompt_bytes} limit={limit}",
            stage_id=contract.stage_id,
            provider=provider,
        )
    if contract.provider_policy.completion_tokens <= 0:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "nonpositive_completion_budget",
            stage_id=contract.stage_id,
            provider=provider,
        )


def _classify_provider_error(
    contract: PlanningStageContract, provider: str, exc: BaseException
) -> tuple[PlanningStageError, bool, object]:
    if isinstance(exc, PlanningStageError):
        return exc, False, None
    failure = classify_provider_failure(provider, exc)
    retry_after = router._last_call_rate_limit_headers.get("retry_after")
    detail = str(exc).replace("\n", " ")[:300]
    lower = detail.lower()
    capacity_markers = (
        "payload_too_large",
        "tpm_capacity",
        "tpm_window",
        "context_length",
        "max_tokens",
        "http_413",
        "status=413",
        "http_429",
        "status=429",
        "rate limit",
        "quota",
    )
    if any(marker in lower for marker in capacity_markers):
        return (
            PlanningStageError(
                PlanningErrorCode.CAPACITY,
                detail,
                stage_id=contract.stage_id,
                provider=provider,
            ),
            False,
            retry_after,
        )
    retryable = (
        failure.telemetry_result in router._TRANSIENT_RESULTS
        or (failure.telemetry_result == "429" and retry_after is not None)
    )
    return (
        PlanningStageError(
            PlanningErrorCode.PROVIDER_TRANSIENT,
            detail,
            stage_id=contract.stage_id,
            provider=provider,
        ),
        retryable,
        retry_after,
    )


def _provider_result(
    provider: str,
    prompt: str,
    model: str,
    contract: PlanningStageContract,
    gemini_key: str,
):
    if provider == "gemini":
        return router._budgeted_provider_call(
            "gemini",
            model,
            router.gemini_json_text,
            gemini_key,
            prompt,
            model=model,
        )
    if provider == "groq":
        # _groq_call asks the compatibility schema seam; that seam now resolves only
        # from _ACTIVE_REQUEST_CONTRACT and cannot inspect the prompt.
        return router._groq_call(prompt)
    if provider == "openrouter":
        return router._openrouter_call_with_repair(
            prompt,
            "openrouter/free",
            "openrouter",
            response_contract=_schema_tuple(contract),
        )
    raise PlanningStageError(
        PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        f"unknown provider={provider}",
        stage_id=contract.stage_id,
    )


def install_planning_contract_router() -> None:
    """Replace the legacy prompt-inferred task router with one explicit-contract owner."""
    if _contains_marker(staged.json_text, _ROUTER_MARKER):
        return

    checkpoint = _load_checkpoint_strict()
    cooldown: set[str] = set()
    transient_cooldown_until: dict[str, float] = {}
    last_call_at: dict[str, float] = {}
    sequence = 0
    gemini_key = router._read_secret_file("GEMINI_API_KEY_FILE")

    # Make any accidental use of the old helper context-backed instead of prompt-backed.
    # This closes the old inference path even inside low-level compatibility utilities.
    router._structured_schema_for_prompt = _explicit_schema_adapter

    def contract_router(_api_key, prompt, model="gemini-2.5-flash"):
        nonlocal sequence
        spec = _ACTIVE_STAGE_SPEC.get()
        if spec is None:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "planning model call has no explicit stage contract",
            )

        effective_prompt = router.with_channel_persona(router._enrich_dialogue_prompt(prompt))
        contract = bind_request_contract(spec, effective_prompt)
        router._CURRENT_REQUEST_META.clear()
        router._CURRENT_REQUEST_META.update(_admission_metadata(contract, effective_prompt))

        cached = _cache_read(checkpoint, contract, model)
        if cached is not None:
            return cached

        providers = list(contract.provider_policy.providers)
        admitted: list[str] = []
        admission_failures: list[PlanningStageError] = []
        for provider in providers:
            try:
                _admit_provider(contract, effective_prompt, provider)
            except PlanningStageError as exc:
                admission_failures.append(exc)
                router._record_attempt(provider, "capacity-preflight", error_detail=str(exc)[:220])
            else:
                admitted.append(provider)
        if not admitted:
            detail = " | ".join(str(exc) for exc in admission_failures)
            raise PlanningStageError(
                PlanningErrorCode.CAPACITY,
                "no provider admitted before invocation: " + detail,
                stage_id=contract.stage_id,
            )

        def run_provider_loop() -> tuple[dict, str]:
            total_attempts = 0
            failures: list[PlanningStageError] = []
            request_token = _ACTIVE_REQUEST_CONTRACT.set(contract)
            try:
                for provider in admitted:
                    if provider in cooldown:
                        router._record_attempt(provider, "circuit-open")
                        continue
                    until = transient_cooldown_until.get(provider)
                    if until is not None and until > time.monotonic():
                        router._record_attempt(provider, "transient-cooldown")
                        continue

                    for provider_attempt in range(contract.provider_policy.max_attempts_per_provider):
                        if total_attempts >= contract.provider_policy.max_total_attempts:
                            break
                        total_attempts += 1
                        since = time.monotonic() - last_call_at.get(provider, 0.0)
                        if since < router.MIN_PROVIDER_CALL_INTERVAL_SECONDS:
                            time.sleep(router.MIN_PROVIDER_CALL_INTERVAL_SECONDS - since)
                        last_call_at[provider] = time.monotonic()
                        try:
                            raw = _provider_result(
                                provider, effective_prompt, model, contract, gemini_key
                            )
                            try:
                                parsed = router._parse_json(raw)
                            except Exception as exc:
                                raise PlanningStageError(
                                    PlanningErrorCode.STRUCTURAL_INVALID,
                                    str(exc)[:300],
                                    stage_id=contract.stage_id,
                                    provider=provider,
                                ) from exc
                            validate_response(contract, parsed)
                        except PlanningStageError as exc:
                            failures.append(exc)
                            router._record_attempt(
                                provider,
                                exc.code.value.lower(),
                                error_detail=str(exc)[:220],
                                duration_seconds=time.monotonic() - last_call_at[provider],
                                provider_attempt=provider_attempt + 1,
                            )
                            # Invalid model output is not replayed against the same
                            # provider; move to fallback. Capacity also moves directly.
                            if exc.code in {
                                PlanningErrorCode.STRUCTURAL_INVALID,
                                PlanningErrorCode.SEMANTIC_INVALID,
                                PlanningErrorCode.CAPACITY,
                                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                            }:
                                break
                            retryable = exc.code == PlanningErrorCode.PROVIDER_TRANSIENT
                            has_retry = provider_attempt + 1 < contract.provider_policy.max_attempts_per_provider
                            if retryable and has_retry and total_attempts < contract.provider_policy.max_total_attempts:
                                delay = router._retry_delay_seconds(provider, provider_attempt)
                                time.sleep(delay)
                                continue
                            transient_cooldown_until[provider] = (
                                time.monotonic() + router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                            )
                            break
                        except Exception as exc:
                            classified, retryable, retry_after = _classify_provider_error(
                                contract, provider, exc
                            )
                            failures.append(classified)
                            failure = classify_provider_failure(provider, exc)
                            router._record_attempt(
                                provider,
                                failure.telemetry_result,
                                error_detail=str(classified)[:220],
                                duration_seconds=time.monotonic() - last_call_at[provider],
                                provider_attempt=provider_attempt + 1,
                            )
                            if failure.open_circuit:
                                cooldown.add(provider)
                            has_retry = provider_attempt + 1 < contract.provider_policy.max_attempts_per_provider
                            if retryable and has_retry and total_attempts < contract.provider_policy.max_total_attempts:
                                delay = router._retry_delay_seconds(
                                    provider, provider_attempt, retry_after
                                )
                                time.sleep(delay)
                                continue
                            if retryable:
                                transient_cooldown_until[provider] = (
                                    time.monotonic() + router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                                )
                            break
                        else:
                            router._record_provider_used(provider)
                            router._record_attempt(
                                provider,
                                "success",
                                duration_seconds=time.monotonic() - last_call_at[provider],
                                provider_attempt=provider_attempt + 1,
                            )
                            print(
                                "Planning subtask provider selected: "
                                f"{provider} stage={contract.stage_id} contract={contract.contract_id}"
                            )
                            return parsed, provider

                if not failures:
                    raise PlanningStageError(
                        PlanningErrorCode.PROVIDER_TRANSIENT,
                        "all admitted providers unavailable by bounded circuit/cooldown policy",
                        stage_id=contract.stage_id,
                    )
                last = failures[-1]
                summary = " | ".join(str(item) for item in failures)
                raise PlanningStageError(
                    last.code,
                    f"all providers exhausted after {total_attempts}/{contract.provider_policy.max_total_attempts} attempts: {summary}",
                    stage_id=contract.stage_id,
                )
            finally:
                _ACTIVE_REQUEST_CONTRACT.reset(request_token)

        active = get_active_budget_task()
        if active is None or active.spec.kind != "OUTLINE_PLAN":
            payload, provider = run_provider_loop()
        else:
            sequence += 1
            child = TaskSpec(
                task_id=f"{active.spec.task_id}_{active.spec.priority.name}_SUBTASK_{sequence:03d}",
                kind="PLANNING_SUBTASK",
                priority=active.spec.priority,
                capability=active.spec.capability,
                max_provider_attempts=contract.provider_policy.max_total_attempts,
                schema_repair_allowed=active.spec.schema_repair_allowed,
                local_fallback=False,
                semantic_block_is_final=False,
            )
            with budget_task_scope(active.ledger, child, requested_model=active.requested_model):
                payload, provider = run_provider_loop()

        # The only durable cache write is here, after successful structural + semantic
        # validation and after provider ownership has completed.
        _cache_commit(checkpoint, contract, model, payload, provider)
        return payload

    setattr(contract_router, _ROUTER_MARKER, True)
    staged.json_text = contract_router
    print(
        "Explicit Planning Stage Contract router installed: prompt inference disabled; "
        "admission precedes provider contact; structural+semantic validation precedes the single cache write"
    )


def _contains_marker(callable_obj, marker: str, seen: set[int] | None = None) -> bool:
    if not callable(callable_obj):
        return False
    if getattr(callable_obj, marker, False):
        return True
    seen = set() if seen is None else seen
    identity = id(callable_obj)
    if identity in seen:
        return False
    seen.add(identity)
    wrapped = getattr(callable_obj, "__wrapped__", None)
    if callable(wrapped) and _contains_marker(wrapped, marker, seen):
        return True
    closure = getattr(callable_obj, "__closure__", None)
    if closure:
        for cell in closure:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value) and _contains_marker(value, marker, seen):
                return True
    return False


@contextmanager
def request_stage_scope(spec: PlanningStageSpec) -> Iterator[None]:
    token = _ACTIVE_STAGE_SPEC.set(spec)
    try:
        yield
    finally:
        _ACTIVE_STAGE_SPEC.reset(token)


@contextmanager
def script_repair_subrequest_scope(expected_ids: list[str]) -> Iterator[None]:
    kind = _ACTIVE_STAGE_KIND.get()
    if kind not in {"full_script", "script_doctor"}:
        kind = "script_doctor"
    with request_stage_scope(script_stage_spec(kind, expected_ids)):
        yield


@contextmanager
def append_subrequest_scope(expected_ids: list[str]) -> Iterator[None]:
    with request_stage_scope(append_stage_spec(expected_ids)):
        yield


def _wrap_stage_parent(name: str, stage_kind: str) -> None:
    current = getattr(staged, name)
    if getattr(current, _STAGE_WRAPPER_MARKER, None) == stage_kind:
        return

    @functools.wraps(current)
    def wrapped(*args, **kwargs):
        token = _ACTIVE_STAGE_KIND.set(stage_kind)
        try:
            return current(*args, **kwargs)
        finally:
            _ACTIVE_STAGE_KIND.reset(token)

    setattr(wrapped, _STAGE_WRAPPER_MARKER, stage_kind)
    setattr(staged, name, wrapped)


def _wrap_outline() -> None:
    current = staged._outline
    if getattr(current, _STAGE_WRAPPER_MARKER, None) == "editorial_outline":
        return

    @functools.wraps(current)
    def wrapped(*args, **kwargs):
        fmt = kwargs.get("fmt")
        if fmt is None:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "outline wrapper could not resolve fmt",
                stage_id="planning.editorial_outline",
            )
        expected = staged._SECTION_COUNTS.get(fmt)
        if not isinstance(expected, int):
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"outline unknown fmt={fmt}",
                stage_id="planning.editorial_outline",
            )
        with request_stage_scope(outline_stage_spec(expected)):
            return current(*args, **kwargs)

    setattr(wrapped, _STAGE_WRAPPER_MARKER, "editorial_outline")
    staged._outline = wrapped


def _wrap_schema_call() -> None:
    current = staged._call_with_schema_repair
    if getattr(current, _STAGE_WRAPPER_MARKER, None) == "script_schema_call":
        return

    @functools.wraps(current)
    def wrapped(api_key, prompt, model, *, expected_ids):
        kind = _ACTIVE_STAGE_KIND.get()
        if kind not in {"full_script", "script_doctor"}:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"schema call has no explicit parent stage kind={kind}",
            )
        with request_stage_scope(script_stage_spec(kind, list(expected_ids))):
            return current(api_key, prompt, model, expected_ids=expected_ids)

    setattr(wrapped, _STAGE_WRAPPER_MARKER, "script_schema_call")
    staged._call_with_schema_repair = wrapped


def _append_target_ids(sections: list) -> list[str]:
    minimum = repair_dossier.FILM_SECTION_MIN_WORDS
    return [
        str(section.id)
        for section in sections
        if staged._word_count(section.narration) < minimum
    ]


def _wrap_append_stage() -> None:
    current = staged._script_doctor_underlength_retry
    if getattr(current, _STAGE_WRAPPER_MARKER, None) == "append_only_repair":
        return

    @functools.wraps(current)
    def wrapped(*args, **kwargs):
        sections = kwargs.get("sections")
        if not isinstance(sections, list):
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "append wrapper could not resolve sections",
                stage_id="planning.append_only_repair",
            )
        ids = _append_target_ids(sections)
        if not ids:
            # Let the existing deterministic guard produce its precise business error
            # without constructing a fake provider contract.
            return current(*args, **kwargs)
        token = _ACTIVE_STAGE_KIND.set("append_only_repair")
        try:
            with request_stage_scope(append_stage_spec(ids)):
                return current(*args, **kwargs)
        finally:
            _ACTIVE_STAGE_KIND.reset(token)

    setattr(wrapped, _STAGE_WRAPPER_MARKER, "append_only_repair")
    staged._script_doctor_underlength_retry = wrapped


def _wrap_section_repair() -> None:
    current = staged._repair_section
    if getattr(current, _STAGE_WRAPPER_MARKER, None) == "section_repair":
        return

    @functools.wraps(current)
    def wrapped(*args, **kwargs):
        section_id = kwargs.get("section_id")
        with request_stage_scope(section_repair_stage_spec(str(section_id or ""))):
            return current(*args, **kwargs)

    setattr(wrapped, _STAGE_WRAPPER_MARKER, "section_repair")
    staged._repair_section = wrapped


def install_planning_stage_boundaries() -> None:
    """Bind explicit stage identity at final Engine/Runner call boundaries."""
    _wrap_outline()
    _wrap_stage_parent("_write_full_script", "full_script")
    _wrap_stage_parent("_script_doctor", "script_doctor")
    _wrap_schema_call()
    _wrap_append_stage()
    _wrap_section_repair()


def assert_planning_stage_contract_installed() -> None:
    if not _contains_marker(staged.json_text, _ROUTER_MARKER):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "explicit contract router is not reachable from resilient_planner.json_text",
        )
    required = {
        "_outline": "editorial_outline",
        "_write_full_script": "full_script",
        "_script_doctor": "script_doctor",
        "_call_with_schema_repair": "script_schema_call",
        "_script_doctor_underlength_retry": "append_only_repair",
        "_repair_section": "section_repair",
    }
    for name, marker in required.items():
        if not _contains_marker(getattr(staged, name), _STAGE_WRAPPER_MARKER):
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"missing explicit stage boundary wrapper:{name}:{marker}",
            )
