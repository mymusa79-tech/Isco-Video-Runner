from __future__ import annotations

"""Bounded recovery for Planning structured-output exhaustion.

Production evidence from Run #206 showed the explicit Stage Contract doing the right
provider sweep, but every transport could still fail for a different reason:
Gemini truncated the outline at the Groq-sized 2400-token transport budget, Groq's
20b model rejected the strict JSON generation, and OpenRouter was already circuit-open
from preflight spend-cap evidence. Repeating the same transport shape on the second
sweep wastes the reserved recovery budget.

This closure changes transport only. It does not alter the outline schema, semantic
validation, section counts, quality gates, provider order, outer attempt ceilings,
OpenRouter circuit policy, or cache authority.
"""

import hashlib
import inspect

from scripts import planning_stage_contract as stage_contract
from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as run125
from scripts import task_level_planner_router as router


_INSTALLED = False
_OUTLINE_CONTRACT = "editorial_outline"
_GEMINI_OUTLINE_FIRST_TOKENS = 4096
_GEMINI_OUTLINE_RECOVERY_TOKENS = 6144
_OUTLINE_COMPACTION_MARKER = "ISCO_OUTLINE_OUTPUT_COMPACTION_V1"
_OUTLINE_COMPACTION_SUFFIX = f"""

{_OUTLINE_COMPACTION_MARKER}
Keep the required JSON semantically complete but concise so transport budget is spent
on all required fields rather than prose expansion. Preserve every required field and
exact section_briefs count. Prefer short scalar values: titles/thumbnail concepts one
line, editorial-intent fields one or two sentences, purpose one sentence, visual_query
a compact stock-search phrase, on_screen_text a short phrase, emotion a short label.
Do not omit, merge, invent, or relax any required field, id, evidence boundary, count,
or editorial requirement.
""".strip()

# Request hash -> number of proven Gemini truncations in this process. This is run-local
# transport state only; it is never persisted and contains no prompt or response text.
_GEMINI_OUTLINE_TRUNCATIONS: dict[str, int] = {}


def _contract_name(prompt: str = "") -> str:
    meta = getattr(router, "_CURRENT_REQUEST_META", {})
    if isinstance(meta, dict):
        name = str(meta.get("response_contract") or "").strip()
        if name:
            return name
    try:
        contract = router._legacy_schema_hint(prompt)
    except Exception:
        contract = None
    return str(contract[0]).strip() if contract else ""


def _request_key(prompt: str) -> str:
    meta = getattr(router, "_CURRENT_REQUEST_META", {})
    if isinstance(meta, dict):
        value = str(meta.get("input_hash") or "").strip()
        if value:
            return value
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _compact_outline_prompt(prompt: str) -> str:
    if _OUTLINE_COMPACTION_MARKER in prompt:
        return prompt
    return prompt.rstrip() + "\n\n" + _OUTLINE_COMPACTION_SUFFIX + "\n"


def _is_groq_schema_generation_failure(error: BaseException | str) -> bool:
    lower = str(error).lower()
    return (
        "groq_json_validate_failed" in lower
        or "code=json_validate_failed" in lower
        or "structured_generation_failed" in lower
        or "failed to validate json" in lower
    )


def _certify_transport_composition() -> None:
    """Fail closed before provider work if the transport assumptions drift."""
    try:
        inspect.signature(router.gemini_json_text, follow_wrapped=False).bind(
            "key",
            "prompt",
            model="model",
            max_output_tokens=1,
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            "PLANNING_STRUCTURED_OUTPUT_CONTRACT_DRIFT "
            f"gemini_adapter_missing_max_output_tokens detail={exc}"
        ) from exc

    production_pool = tuple(run125._GROQ_MODEL_POOL)
    required = ("openai/gpt-oss-20b", "openai/gpt-oss-120b")
    if any(model not in production_pool for model in required):
        raise RuntimeError(
            "PLANNING_STRUCTURED_OUTPUT_CONTRACT_DRIFT "
            f"groq_strict_model_pool={production_pool!r}"
        )

    probe = capacity._response_format_for_contract(
        (_OUTLINE_CONTRACT, {"type": "object", "properties": {}, "additionalProperties": False})
    )
    schema = probe.get("json_schema") if isinstance(probe, dict) else None
    if (
        not isinstance(schema, dict)
        or schema.get("strict") is not True
        or probe.get("type") != "json_schema"
    ):
        raise RuntimeError(
            "PLANNING_STRUCTURED_OUTPUT_CONTRACT_DRIFT groq_outline_strict_schema_disabled"
        )


def install_planning_structured_output_family() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    _certify_transport_composition()

    # Gemini is not constrained by Groq's 8K TPM envelope. Give the outline its own
    # provider-specific completion budget. A second sweep changes the failed dimension:
    # only after a proven truncation does the same request receive the larger bounded
    # recovery ceiling. Every downstream schema/semantic gate remains unchanged.
    original_gemini = router.gemini_json_text

    def gemini_with_provider_budget(
        api_key: str,
        prompt: str,
        model: str = "gemini-2.5-flash",
        *,
        max_output_tokens: int | None = None,
        **kwargs,
    ):
        if _contract_name(prompt) != _OUTLINE_CONTRACT:
            return original_gemini(
                api_key,
                prompt,
                model=model,
                max_output_tokens=max_output_tokens,
                **kwargs,
            )

        key = _request_key(prompt)
        prior_truncations = _GEMINI_OUTLINE_TRUNCATIONS.get(key, 0)
        provider_budget = (
            _GEMINI_OUTLINE_RECOVERY_TOKENS
            if prior_truncations > 0
            else _GEMINI_OUTLINE_FIRST_TOKENS
        )
        if max_output_tokens is not None:
            provider_budget = max(provider_budget, int(max_output_tokens))
        router._last_call_response_meta["provider_completion_tokens"] = provider_budget
        router._last_call_response_meta["structured_output_recovery_round"] = (
            "recovery" if prior_truncations > 0 else "initial"
        )
        try:
            return original_gemini(
                api_key,
                _compact_outline_prompt(prompt),
                model=model,
                max_output_tokens=provider_budget,
                **kwargs,
            )
        except Exception as exc:
            if "gemini_interaction_output_truncated" in str(exc).lower():
                _GEMINI_OUTLINE_TRUNCATIONS[key] = prior_truncations + 1
            raise

    setattr(gemini_with_provider_budget, "_isco_planning_structured_output_family", True)
    router.gemini_json_text = gemini_with_provider_budget

    # Run125 already owns model-diverse Groq routing. Extend only its model-specific
    # capability predicate for the exact outline schema failure seen in production.
    # 20b json_validate_failed therefore advances to the already-certified 120b model
    # inside the same bounded Groq attempt instead of waiting for the outer second sweep
    # to hit 20b again. No preview model is added to the production pool.
    original_model_unavailable = run125._is_model_unavailable

    def model_unavailable_or_outline_schema(error) -> bool:
        if original_model_unavailable(error):
            return True
        return (
            _contract_name() == _OUTLINE_CONTRACT
            and _is_groq_schema_generation_failure(error)
        )

    run125._is_model_unavailable = model_unavailable_or_outline_schema

    # Apply the same concise-output transport hint to Groq without changing its 2400
    # completion reserve or strict schema. This lowers structured-generation pressure
    # while keeping the exact semantic transaction and validation authority intact.
    original_groq = router._groq_call

    def groq_with_outline_compaction(prompt: str) -> dict:
        if _contract_name(prompt) == _OUTLINE_CONTRACT:
            prompt = _compact_outline_prompt(prompt)
        return original_groq(prompt)

    router._groq_call = groq_with_outline_compaction

    _INSTALLED = True
    print(
        "Planning structured-output family closure installed: "
        "gemini_outline_tokens=4096->6144_on_proven_truncation "
        "groq_outline_schema_failure=model_diverse_failover "
        "outline_compaction=transport_only strict_validation=unchanged "
        "outer_attempt_budget=unchanged openrouter_circuit=unchanged"
    )
