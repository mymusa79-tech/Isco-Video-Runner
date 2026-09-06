from __future__ import annotations

"""Explicit request contracts for every Planning model call.

Stage identity is bound at stable Python call boundaries. Prompt text is input data only:
it never selects a schema, semantic contract, provider policy, or cache policy.
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
    # Run #142 reserves a second provider pass for outline-style contracts. Run #210
    # tightens the rule: the first pass always tries independent provider families in
    # order; a second pass may revisit only providers whose own first-pass failure was
    # genuinely retryable/transient. A CAPACITY/STRUCTURAL/SEMANTIC/INTERNAL failure on
    # one provider must neither be retried for that provider nor suppress an unrelated
    # transient provider's bounded retry slot.
    second_pass_after_full_exhaustion: bool = False
    # Run #209 (and the identical Run #204/#205 before the outline split): 2400 is sized
    # from Groq's own 8000 TPM ceiling (confirmed razor-thin - Run #208 hit
    # GROQ_TPM_WINDOW_BUSY_PRECHECK at that same budget for Film/Long), but Gemini has no
    # such shared-window constraint at all and was truncating (GEMINI_INTERACTION_
    # OUTPUT_TRUNCATED/INCOMPLETE_MAX_TOKENS) on Film's richer editorial_intent content
    # even after the outline was split into core+sections. Optional per-provider override
    # so a provider whose real ceiling is unrelated to Groq's math can get real headroom
    # without touching Groq's own budget.
    completion_tokens_by_provider: tuple[tuple[str, int], ...] = ()

    def prompt_limit(self, provider: str) -> int | None:
        return dict(self.max_prompt_utf8_bytes).get(provider)

    def completion_tokens_for(self, provider: str) -> int:
        return dict(self.completion_tokens_by_provider).get(provider, self.completion_tokens)


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

# Run #140 showed that a 3200-token outline reserve made the exact 20.6 KiB P0
# envelope require 8306 Groq TPM, leaving Gemini as the only real provider while
# OpenRouter was unavailable. 2400 is the already-established outline transport budget
# in provider_capacity_hardening and keeps the same strict JSON/semantic contract while
# restoring Groq portability (7506 estimated tokens for the Run #140 envelope).
OUTLINE_COMPLETION_TOKEN_BUDGET = 2400
OUTLINE_MAX_ATTEMPTS_PER_PROVIDER = 1
# Two one-contact-per-family passes fit inside the same existing global ceiling. The
# second pass is selective: only providers whose first-pass failure is retry-eligible
# may appear again, so no provider is retried back-to-back before alternatives are tried.
OUTLINE_MAX_TOTAL_ATTEMPTS = len(_PROVIDER_ORDER) * 2

# One explicit provider-output budget per bounded Planning transport contract.  These
# names are produced from stage identity + expected ids below; prompt text has no role
# in selecting either the name or the budget.  Run123 consumes this same table for its
# latency/provider adapters, so the Stage Contract remains the single policy owner.
SHARD_COMPLETION_TOKEN_BUDGETS = {
    "script_writer_1": 900,
    "script_writer_2": 1300,
    "script_writer_3": 1800,
    "script_doctor_1": 900,
    "script_doctor_2": 1400,
    "script_doctor_3": 1800,
    "dossier_repair_1": 850,
    "dossier_repair_2": 1400,
    "append_repair_1": 600,
    "append_repair_2": 800,
    "append_repair_3": 1000,
    "append_repair_4": 1200,
    "append_repair_5": 1400,
    "append_repair_6": 1600,
    "append_repair_7": 1800,
    "append_repair_8": 2000,
}


def _transport_completion_tokens(profile: str, expected_items: int) -> int:
    if profile == "editorial_outline":
        return OUTLINE_COMPLETION_TOKEN_BUDGET
    if profile == "section_repair":
        return 2200
    name = f"{profile}_{expected_items}"
    try:
        return SHARD_COMPLETION_TOKEN_BUDGETS[name]
    except KeyError as exc:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"unsupported bounded Planning transport contract={name}",
        ) from exc


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


def _append_schema(expected: int, *, ordered_subset: bool) -> dict:
    item = _strict_object(
        {"id": {"type": "string"}, "append_text": {"type": "string"}},
        ["id", "append_text"],
    )
    return _strict_object(
        {
            "additions": {
                "type": "array",
                "items": item,
                "minItems": 0 if ordered_subset else expected,
                "maxItems": expected,
            }
        },
        ["additions"],
    )


def _provider_policy(
    completion_tokens: int,
    *,
    max_attempts_per_provider: int | None = None,
    max_total_attempts: int | None = None,
    second_pass_after_full_exhaustion: bool = False,
    completion_tokens_by_provider: tuple[tuple[str, int], ...] = (),
) -> ProviderPolicy:
    attempts_per_provider = (
        router.TRANSIENT_PROVIDER_MAX_ATTEMPTS
        if max_attempts_per_provider is None
        else int(max_attempts_per_provider)
    )
    total_attempts = (
        router.PLANNING_SUBTASK_MAX_PROVIDER_ATTEMPTS
        if max_total_attempts is None
        else int(max_total_attempts)
    )
    if attempts_per_provider <= 0 or total_attempts <= 0:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "provider policy requires positive bounded attempts",
        )
    return ProviderPolicy(
        providers=_PROVIDER_ORDER,
        max_attempts_per_provider=attempts_per_provider,
        max_total_attempts=total_attempts,
        completion_tokens=completion_tokens,
        # Preserve only evidence-backed local limits. Unknown provider limits are not
        # invented; their own preflight owners may reject before HTTP contact.
        max_prompt_utf8_bytes=(("groq", router.GROQ_MAX_PROMPT_UTF8_BYTES),),
        openrouter_compact_repair_max_attempts=1,
        second_pass_after_full_exhaustion=second_pass_after_full_exhaustion,
        completion_tokens_by_provider=completion_tokens_by_provider,
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
            "transport_profile": "editorial_outline",
            "expected_count": expected_count,
            "unique_nonempty_ids": True,
            "nonempty_purpose": True,
            "narrative_identity_gates": True,
        },
        # Provider-first failover: one request per family in the first sweep. If the
        # sweep fails, only providers with a retryable transient outcome can participate
        # in one bounded second sweep; terminal failures on other providers do not veto
        # that independent retry.
        provider_policy=_provider_policy(
            _transport_completion_tokens("editorial_outline", expected_count),
            max_attempts_per_provider=OUTLINE_MAX_ATTEMPTS_PER_PROVIDER,
            max_total_attempts=OUTLINE_MAX_TOTAL_ATTEMPTS,
            second_pass_after_full_exhaustion=True,
        ),
        cache_policy=CachePolicy(),
    )


def outline_stage_spec_for_format(fmt: str) -> PlanningStageSpec:
    """Resolve the Engine-owned outline size into the canonical transport policy."""
    expected = staged._SECTION_COUNTS.get(str(fmt or "").strip().lower())
    if not isinstance(expected, int):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"outline unknown fmt={fmt}",
            stage_id="planning.editorial_outline",
        )
    return outline_stage_spec(expected)


def script_stage_spec(stage_kind: str, expected_ids: list[str]) -> PlanningStageSpec:
    ids = [str(item).strip() for item in expected_ids]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "script contract requires unique nonempty expected ids",
            stage_id=f"planning.{stage_kind}",
        )
    profiles = {
        "full_script": "script_writer",
        "script_doctor": "script_doctor",
        "dossier_repair": "dossier_repair",
    }
    if stage_kind not in profiles:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"unsupported script stage kind={stage_kind}",
        )
    profile = profiles[stage_kind]
    return PlanningStageSpec(
        stage_id=f"planning.{stage_kind}",
        contract_id=f"planning.{stage_kind}.v1",
        output_schema=_script_schema(len(ids)),
        semantic_rules={
            "kind": "script",
            "transport_profile": profile,
            "expected_ids": ids,
            "exact_order": True,
        },
        provider_policy=_provider_policy(
            _transport_completion_tokens(profile, len(ids))
        ),
        cache_policy=CachePolicy(),
    )


def append_stage_spec(
    expected_ids: list[str],
    *,
    allow_ordered_subset: bool = False,
) -> PlanningStageSpec:
    ids = [str(item).strip() for item in expected_ids]
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "append contract requires unique nonempty expected ids",
            stage_id="planning.append_only_repair",
        )
    suffix = "candidate.v1" if allow_ordered_subset else "exact.v1"
    # Append repair is a compound transaction with downstream word-band and aggregate
    # validation. Its provider fragments are deliberately never durable cache authority.
    # This prevents an intermediate/partial repair candidate from becoming reusable state.
    no_fragment_cache = CachePolicy(read=False, write=False)
    return PlanningStageSpec(
        stage_id="planning.append_only_repair",
        contract_id=f"planning.append_only_repair.{suffix}",
        output_schema=_append_schema(len(ids), ordered_subset=allow_ordered_subset),
        semantic_rules={
            "kind": "append",
            "transport_profile": "append_repair",
            "expected_ids": ids,
            "exact_order": not allow_ordered_subset,
            "ordered_subset_allowed": allow_ordered_subset,
            "nonempty_append_text": True,
        },
        provider_policy=_provider_policy(
            _transport_completion_tokens("append_repair", len(ids))
        ),
        cache_policy=no_fragment_cache,
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
        semantic_rules={
            "kind": "section_repair",
            "transport_profile": "section_repair",
            "section_id": section_id,
            "nonempty": True,
        },
        provider_policy=_provider_policy(
            _transport_completion_tokens("section_repair", 1)
        ),
        cache_policy=CachePolicy(),
    )


def bind_request_contract(spec: PlanningStageSpec, effective_prompt: str) -> PlanningStageContract:
    return PlanningStageContract(
        stage_id=spec.stage_id,
        contract_id=spec.contract_id,
        input_hash=hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest(),
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


def _raise_validation(
    code: PlanningErrorCode,
    contract: PlanningStageContract,
    path: str,
    detail: str,
) -> None:
    raise PlanningStageError(code, f"path={path} detail={detail}", stage_id=contract.stage_id)


def _validate_schema(
    value: object,
    schema: dict,
    contract: PlanningStageContract,
    path: str = "$",
) -> None:
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
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            f"$.{label}",
            "duplicate_ids",
        )
    return ids


def _validate_outline_semantics(data: dict, contract: PlanningStageContract) -> None:
    briefs = data.get("section_briefs")
    if not isinstance(briefs, list):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.section_briefs",
            "expected_array",
        )
    _unique_ids(briefs, contract, "section_briefs")
    if any(not str(item.get("purpose") or "").strip() for item in briefs):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.section_briefs",
            "empty_purpose",
        )

    narrative_format = str(data.get("narrative_format") or "").strip()
    if narrative_format not in getattr(staged, "_NARRATIVE_FORMATS", {}):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.narrative_format",
            "unsupported",
        )
    if staged.validate_narrative_format(narrative_format, n=6):
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
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$",
            "empty_identity_variant",
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
    if staged.validate_identity_phrases(opener, closer, n=6):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$",
            "identity_anti_repetition",
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


def _validate_append_semantics(data: dict, contract: PlanningStageContract) -> None:
    additions = data.get("additions")
    if not isinstance(additions, list):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.additions",
            "expected_array",
        )
    returned_ids = _unique_ids(additions, contract, "additions")
    if any(not str(item.get("append_text") or "").strip() for item in additions):
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.additions",
            "empty_append_text",
        )

    expected_ids = list(contract.semantic_rules.get("expected_ids") or [])
    if contract.semantic_rules.get("ordered_subset_allowed"):
        positions = {section_id: index for index, section_id in enumerate(expected_ids)}
        previous = -1
        for section_id in returned_ids:
            position = positions.get(section_id)
            if position is None:
                _raise_validation(
                    PlanningErrorCode.SEMANTIC_INVALID,
                    contract,
                    "$.additions",
                    f"unexpected_id={section_id}",
                )
            if position <= previous:
                _raise_validation(
                    PlanningErrorCode.SEMANTIC_INVALID,
                    contract,
                    "$.additions",
                    "subset_order_changed",
                )
            previous = position
        return

    if returned_ids != expected_ids:
        _raise_validation(
            PlanningErrorCode.SEMANTIC_INVALID,
            contract,
            "$.additions",
            "exact_id_order_mismatch",
        )


def validate_response(contract: PlanningStageContract, data: dict) -> dict:
    _validate_schema(data, contract.output_schema, contract)
    kind = str(contract.semantic_rules.get("kind") or "")
    try:
        if kind == "editorial_outline":
            _validate_outline_semantics(data, contract)
        elif kind == "script":
            staged._parse_full_script_response(
                data,
                list(contract.semantic_rules["expected_ids"]),
            )
        elif kind == "append":
            _validate_append_semantics(data, contract)
        elif kind == "section_repair":
            if not str(data.get("narration") or "").strip():
                _raise_validation(
                    PlanningErrorCode.SEMANTIC_INVALID,
                    contract,
                    "$.narration",
                    "empty",
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
            PlanningErrorCode.CHECKPOINT_INVALID,
            "checkpoint_root_shape_invalid",
        )
    # Contract-v1 intentionally invalidates historical prompt-hash cache authority.
    if data.get("version") != 2:
        return {"version": 2, "responses": {}}
    data.setdefault("responses", {})
    return data


def _save_checkpoint(checkpoint: dict) -> None:
    path = router.CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _evict(checkpoint: dict, key: str, error: PlanningStageError) -> None:
    if key in checkpoint["responses"]:
        checkpoint["responses"].pop(key, None)
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

    valid_envelope = (
        isinstance(row, dict)
        and row.get("stage_id") == contract.stage_id
        and row.get("contract_id") == contract.contract_id
        and row.get("input_hash") == contract.input_hash
        and row.get("contract_fingerprint") == _contract_fingerprint(contract)
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
    """Single durable Planning response-write authority."""
    if not contract.cache_policy.write:
        return
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


def _schema_tuple(
    contract: PlanningStageContract | PlanningStageSpec,
) -> tuple[str, dict]:
    """Return the provider schema name from explicit contract fields only."""
    profile = str(contract.semantic_rules.get("transport_profile") or "").strip()
    if profile in {"editorial_outline", "section_repair"}:
        return profile, contract.output_schema
    if profile in {"script_writer", "script_doctor", "dossier_repair", "append_repair"}:
        expected_ids = list(contract.semantic_rules.get("expected_ids") or [])
        if not expected_ids:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"explicit provider schema has no expected ids profile={profile}",
                stage_id=contract.stage_id,
            )
        return f"{profile}_{len(expected_ids)}", contract.output_schema
    raise PlanningStageError(
        PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        f"unsupported explicit provider transport profile={profile or 'missing'}",
        stage_id=contract.stage_id,
    )


def _explicit_schema_adapter(_prompt: str) -> tuple[str, dict]:
    """Compatibility seam for old provider helpers; prompt content is ignored."""
    owner: PlanningStageContract | PlanningStageSpec | None = _ACTIVE_REQUEST_CONTRACT.get()
    if owner is None:
        # Capacity admission legitimately runs before the provider router binds the
        # effective prompt/input hash.  Its caller must still have installed an exact
        # request StageSpec; that spec owns the same schema and provider policy.
        owner = _ACTIVE_STAGE_SPEC.get()
    if owner is None:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "prompt schema inference is disabled; no active explicit request contract",
        )
    return _schema_tuple(owner)


def active_planning_completion_tokens() -> int | None:
    """Expose only the active explicit provider policy to compatibility adapters."""
    owner: PlanningStageContract | PlanningStageSpec | None = _ACTIVE_REQUEST_CONTRACT.get()
    if owner is None:
        owner = _ACTIVE_STAGE_SPEC.get()
    return None if owner is None else owner.provider_policy.completion_tokens


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


def _provider_failure(
    contract: PlanningStageContract,
    provider: str,
    exc: BaseException,
) -> tuple[PlanningStageError, bool, object, object]:
    failure = router.classify_provider_failure(provider, exc)
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
    code = (
        PlanningErrorCode.CAPACITY
        if any(marker in lower for marker in capacity_markers)
        else PlanningErrorCode.PROVIDER_TRANSIENT
    )
    retryable = (
        code == PlanningErrorCode.PROVIDER_TRANSIENT
        and (
            failure.telemetry_result in router._TRANSIENT_RESULTS
            or (failure.telemetry_result == "429" and retry_after is not None)
        )
    )
    return (
        PlanningStageError(
            code,
            detail,
            stage_id=contract.stage_id,
            provider=provider,
        ),
        retryable,
        retry_after,
        failure,
    )


def _safe_provider_failure(
    contract: PlanningStageContract,
    provider: str,
    exc: BaseException,
) -> tuple[PlanningStageError, bool, object, object]:
    """_provider_failure calls the same live-patched classify_provider_failure chain
    task_level_planner_router.py's task_router defends against in its own
    run_provider_loop (run120/122/123/124/125 each replace it in turn): a defect in
    that bookkeeping must never be able to crash the request it is classifying instead
    of the real provider failure. Degrade to a generic, non-retryable classification.
    """
    try:
        return _provider_failure(contract, provider, exc)
    except Exception as classify_exc:
        print(
            "Planning provider failure classification error "
            f"(treating as generic failure): {provider}:{type(classify_exc).__name__}"
        )
        detail = str(exc).replace("\n", " ")[:300]
        return (
            PlanningStageError(
                PlanningErrorCode.PROVIDER_TRANSIENT,
                detail,
                stage_id=contract.stage_id,
                provider=provider,
            ),
            False,
            None,
            router.ProviderFailure("classification_error", router.AttemptOutcome.OTHER, False),
        )


def _safe_record_attempt(*args, **kwargs) -> None:
    """Telemetry recording (also live-patched by run123/125) must never crash the
    request it is only supposed to be observing."""
    try:
        router._record_attempt(*args, **kwargs)
    except Exception as record_exc:
        provider = args[0] if args else kwargs.get("provider_name", "?")
        print(
            "Planning provider attempt-recording error "
            f"(continuing without telemetry): {provider}:{type(record_exc).__name__}"
        )


def _provider_result(
    provider: str,
    prompt: str,
    model: str,
    contract: PlanningStageContract,
    primary_api_key: str,
):
    if provider == "gemini":
        # Engine config.secret() deliberately consumes and deletes the one-time file
        # before build_plan().  Its api_key argument is therefore the only canonical
        # request-scoped credential at this boundary; re-reading *_FILE here breaks the
        # secure one-time lifecycle and caused the uploaded Production run to fail
        # before any provider request was made.
        gemini_key = str(primary_api_key or "").strip()
        if not gemini_key:
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                "Gemini request credential unavailable after one-time secret consumption",
                stage_id=contract.stage_id,
                provider=provider,
            )
        return router._budgeted_provider_call(
            "gemini",
            model,
            router.gemini_json_text,
            gemini_key,
            prompt,
            model=model,
            max_output_tokens=contract.provider_policy.completion_tokens_for("gemini"),
        )
    if provider == "groq":
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
    """Replace prompt-inferred routing with one explicit-contract request owner."""
    if _contains_marker(staged.json_text, _ROUTER_MARKER):
        # Idempotent lifecycle reassertion must also restore the one schema authority;
        # a historical installer is not allowed to survive merely because the routed
        # json_text wrapper was already present.
        router._structured_schema_for_prompt = _explicit_schema_adapter
        return

    checkpoint = _load_checkpoint_strict()
    cooldown: set[str] = set()
    transient_cooldown_until: dict[str, float] = {}
    last_call_at: dict[str, float] = {}
    sequence = 0

    # Every old low-level caller that still asks for a response schema now resolves it
    # from the active request contract. The prompt argument is deliberately ignored.
    router._structured_schema_for_prompt = _explicit_schema_adapter

    def contract_router(api_key, prompt, model="gemini-2.5-flash"):
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

        admitted: list[str] = []
        admission_failures: list[PlanningStageError] = []
        for provider in contract.provider_policy.providers:
            try:
                _admit_provider(contract, effective_prompt, provider)
            except PlanningStageError as exc:
                admission_failures.append(exc)
                router._record_attempt(
                    provider,
                    "capacity-preflight",
                    error_detail=str(exc)[:220],
                )
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
            deferred_retry_providers: set[str] = set()
            request_token = _ACTIVE_REQUEST_CONTRACT.set(contract)
            try:
                round_index = 0
                while True:
                    round_index += 1
                    providers_this_round = (
                        admitted
                        if round_index == 1
                        else [
                            provider
                            for provider in admitted
                            if provider in deferred_retry_providers
                        ]
                    )
                    if not providers_this_round:
                        break

                    for provider in providers_this_round:
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
                            started = time.monotonic()
                            last_call_at[provider] = started
                            try:
                                raw = _provider_result(
                                    provider,
                                    effective_prompt,
                                    model,
                                    contract,
                                    api_key,
                                )
                                try:
                                    parsed = raw if isinstance(raw, dict) else router._parse_json(raw)
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
                                _safe_record_attempt(
                                    provider,
                                    exc.code.value.lower(),
                                    error_detail=str(exc)[:220],
                                    duration_seconds=time.monotonic() - started,
                                    provider_attempt=provider_attempt + 1,
                                )
                                # Invalid output, fixed capacity, and contract failures
                                # are terminal for this provider in this request.
                                if exc.code in {
                                    PlanningErrorCode.STRUCTURAL_INVALID,
                                    PlanningErrorCode.SEMANTIC_INVALID,
                                    PlanningErrorCode.CAPACITY,
                                    PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                                }:
                                    break
                                has_retry = provider_attempt + 1 < contract.provider_policy.max_attempts_per_provider
                                if (
                                    exc.code == PlanningErrorCode.PROVIDER_TRANSIENT
                                    and has_retry
                                    and total_attempts < contract.provider_policy.max_total_attempts
                                ):
                                    time.sleep(router._retry_delay_seconds(provider, provider_attempt))
                                    continue
                                if (
                                    exc.code == PlanningErrorCode.PROVIDER_TRANSIENT
                                    and round_index == 1
                                    and contract.provider_policy.second_pass_after_full_exhaustion
                                    and total_attempts < contract.provider_policy.max_total_attempts
                                ):
                                    deferred_retry_providers.add(provider)
                                transient_cooldown_until[provider] = (
                                    time.monotonic() + router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                                )
                                break
                            except Exception as exc:
                                classified, retryable, retry_after, failure = _safe_provider_failure(
                                    contract,
                                    provider,
                                    exc,
                                )
                                failures.append(classified)
                                _safe_record_attempt(
                                    provider,
                                    failure.telemetry_result,
                                    error_detail=str(classified)[:220],
                                    duration_seconds=time.monotonic() - started,
                                    provider_attempt=provider_attempt + 1,
                                )
                                if failure.open_circuit:
                                    cooldown.add(provider)
                                has_retry = provider_attempt + 1 < contract.provider_policy.max_attempts_per_provider
                                if (
                                    retryable
                                    and has_retry
                                    and total_attempts < contract.provider_policy.max_total_attempts
                                ):
                                    time.sleep(
                                        router._retry_delay_seconds(
                                            provider,
                                            provider_attempt,
                                            retry_after,
                                        )
                                    )
                                    continue
                                if retryable:
                                    if (
                                        round_index == 1
                                        and contract.provider_policy.second_pass_after_full_exhaustion
                                        and not failure.open_circuit
                                        and total_attempts < contract.provider_policy.max_total_attempts
                                    ):
                                        deferred_retry_providers.add(provider)
                                    transient_cooldown_until[provider] = (
                                        time.monotonic() + router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS
                                    )
                                break
                            else:
                                router._record_provider_used(provider)
                                router._record_attempt(
                                    provider,
                                    "success",
                                    duration_seconds=time.monotonic() - started,
                                    provider_attempt=provider_attempt + 1,
                                )
                                print(
                                    "Planning subtask provider selected: "
                                    f"{provider} stage={contract.stage_id} contract={contract.contract_id}"
                                )
                                return parsed, provider

                    # First pass always gives every admitted family its independent shot.
                    # Only after that sweep do we wait once and retry the providers that
                    # themselves proved transient/retryable. Terminal failure on another
                    # family is local to that family and cannot veto this bounded retry.
                    if (
                        contract.provider_policy.second_pass_after_full_exhaustion
                        and round_index == 1
                        and deferred_retry_providers
                        and total_attempts < contract.provider_policy.max_total_attempts
                    ):
                        # The deferred round owns this bounded cooldown. Clear only the
                        # provider-local timestamps for providers explicitly authorized
                        # for round two so the same cooldown is not enforced twice.
                        time.sleep(router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS)
                        for provider in deferred_retry_providers:
                            transient_cooldown_until.pop(provider, None)
                        continue
                    break

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

        # Exactly one response-cache write authority exists, and it is reached only
        # after the complete structural + semantic request contract has passed.
        _cache_commit(checkpoint, contract, model, payload, provider)
        return payload

    setattr(contract_router, _ROUTER_MARKER, True)
    staged.json_text = contract_router
    print(
        "Explicit Planning Stage Contract router installed: prompt inference disabled; "
        "admission precedes provider contact; provider-first selective transient retry; "
        "structural+semantic validation precedes the single cache write"
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
    if kind not in {"full_script", "script_doctor", "dossier_repair"}:
        kind = "script_doctor"
    with request_stage_scope(script_stage_spec(kind, expected_ids)):
        yield


@contextmanager
def script_batch_scope(label: str, expected_ids: list[str]) -> Iterator[None]:
    """Bind Writer/Doctor identity before pre-provider capacity admission."""
    kinds = {"writer": "full_script", "doctor": "script_doctor"}
    try:
        kind = kinds[label]
    except KeyError as exc:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            f"unsupported bounded script transport label={label}",
        ) from exc
    token = _ACTIVE_STAGE_KIND.set(kind)
    try:
        with request_stage_scope(script_stage_spec(kind, expected_ids)):
            yield
    finally:
        _ACTIVE_STAGE_KIND.reset(token)


@contextmanager
def dossier_repair_subrequest_scope(expected_ids: list[str]) -> Iterator[None]:
    """Bind the orchestrator-level RepairDossier transport explicitly."""
    token = _ACTIVE_STAGE_KIND.set("dossier_repair")
    try:
        with request_stage_scope(script_stage_spec("dossier_repair", expected_ids)):
            yield
    finally:
        _ACTIVE_STAGE_KIND.reset(token)


@contextmanager
def append_subrequest_scope(expected_ids: list[str]) -> Iterator[None]:
    with request_stage_scope(append_stage_spec(expected_ids)):
        yield


@contextmanager
def append_candidate_subrequest_scope(expected_ids: list[str]) -> Iterator[None]:
    with request_stage_scope(
        append_stage_spec(expected_ids, allow_ordered_subset=True)
    ):
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
        fmt = kwargs.get("fmt", args[2] if len(args) > 2 else None)
        with request_stage_scope(outline_stage_spec_for_format(fmt)):
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
        if kind not in {"full_script", "script_doctor", "dossier_repair"}:
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
            return current(*args, **kwargs)
        current_words = kwargs.get("current_words")
        minimum = kwargs.get("minimum")
        ordered_subset = (
            isinstance(current_words, (int, float))
            and isinstance(minimum, (int, float))
            and current_words < minimum
        )
        token = _ACTIVE_STAGE_KIND.set("append_only_repair")
        try:
            with request_stage_scope(
                append_stage_spec(ids, allow_ordered_subset=ordered_subset)
            ):
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
        section_id = kwargs.get("section_id", args[2] if len(args) > 2 else None)
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
    if router._structured_schema_for_prompt is not _explicit_schema_adapter:
        raise PlanningStageError(
            PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
            "explicit Planning schema adapter lost authority",
        )
    required = (
        "_outline",
        "_write_full_script",
        "_script_doctor",
        "_call_with_schema_repair",
        "_script_doctor_underlength_retry",
        "_repair_section",
    )
    for name in required:
        if not _contains_marker(getattr(staged, name), _STAGE_WRAPPER_MARKER):
            raise PlanningStageError(
                PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
                f"missing explicit stage boundary wrapper:{name}",
            )
