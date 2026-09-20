from __future__ import annotations

"""Clean V2 Text Audit extensions: factuality + tone/naturalness, each with Mistral last."""

import threading
from typing import Any

from .mistral_executor import (
    MistralExecutorWireFailure,
    mistral_executor_json,
)


TEXT_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "block"]},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "professional_advice_flags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "expert_persona_flags": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "unsupported_claims",
        "professional_advice_flags",
        "expert_persona_flags",
        "notes",
    ],
    "additionalProperties": False,
}


TONE_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "block"]},
        "preachiness_flags": {"type": "array", "items": {"type": "string"}},
        "cultural_dignity_flags": {"type": "array", "items": {"type": "string"}},
        "naturalness_flags": {"type": "array", "items": {"type": "string"}},
        "narrative_format_flags": {"type": "array", "items": {"type": "string"}},
        "unverified_religious_quote_flags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "preachiness_flags",
        "cultural_dignity_flags",
        "naturalness_flags",
        "narrative_format_flags",
        "unverified_religious_quote_flags",
        "notes",
    ],
    "additionalProperties": False,
}

_EXPECTED_BASE_ROUTE = ("gemini", "groq", "openrouter")
_AUDIT_ROUTE_LOCK = threading.RLock()

_LEGACY_PROFESSIONAL_ADVICE_RULE = (
    "3. Flag diagnosis, treatment, prescriptions, individualized medical/wellness advice, or language presenting the narrator\n"
    "   as a doctor, therapist, psychologist, lawyer, financial adviser, political expert, imam or religious scholar."
)
_PRODUCTIVITY_SCOPE_CLARIFICATION = (
    "\n   For professional_advice_flags specifically: do NOT flag ordinary general productivity/self-improvement advice "
    "(for example: choose a task, set a reminder, or track progress). Only flag individualized medical, legal, financial, "
    "or religious authority advice/claims, treatment or prescription content, or claims that the narrator holds such "
    "professional/religious authority."
)


def _scope_professional_advice_prompt(prompt: str) -> str:
    """Narrow only professional-advice semantics while preserving the legacy audit contract."""
    if _LEGACY_PROFESSIONAL_ADVICE_RULE not in prompt:
        raise RuntimeError("Clean V2 factuality professional-advice rule drift")
    return prompt.replace(
        _LEGACY_PROFESSIONAL_ADVICE_RULE,
        _LEGACY_PROFESSIONAL_ADVICE_RULE + _PRODUCTIVITY_SCOPE_CLARIFICATION,
        1,
    )


def _validate_factuality_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(
            result,
            required_arrays=(
                "unsupported_claims",
                "professional_advice_flags",
                "expert_persona_flags",
                "notes",
            ),
        )
    except Exception as exc:
        raise MistralExecutorWireFailure(
            f"mistral invalid json text audit contract {type(exc).__name__.lower()}"
        ) from exc
    return result


def _mistral_factuality_call(prompt: str) -> dict[str, Any]:
    return _validate_factuality_result(
        mistral_executor_json(
            prompt,
            max_tokens=2200,
            task_kind="text_audit",
            response_schema=("clean_v2_factuality_audit_v1", TEXT_AUDIT_SCHEMA),
            temperature=0.1,
        )
    )


def audit_plan_with_mistral(
    api_key: str,
    plan: object,
    research_context: object,
    model: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the frozen Engine audit with one final executor-only Mistral route.

    The Engine remains authoritative for the full prompt, normalization, validation,
    fail-closed result, and Approval Shopping behavior.  The scoped binding changes
    only the provider list for this Clean V2 call and is restored in all outcomes.
    """
    from isco_video_agent import factuality
    from isco_video_agent import text_audit_router

    with _AUDIT_ROUTE_LOCK:
        original_route = factuality.route_text_audit

        def route_with_final_mistral(
            providers,
            prompt: str,
            *,
            cooldown: set[str] | None = None,
        ):
            names = tuple(str(name) for name, _call in providers)
            if names != _EXPECTED_BASE_ROUTE:
                raise RuntimeError(
                    "Clean V2 factuality base provider route drift: " + "->".join(names)
                )
            def contract_validated(call):
                def invoke(value: str):
                    return _validate_factuality_result(call(value))

                return invoke

            scoped_prompt = _scope_professional_advice_prompt(prompt)
            extended = [
                (name, contract_validated(call)) for name, call in providers
            ]
            extended.append(("mistral", _mistral_factuality_call))
            return text_audit_router.route_text_audit(
                extended,
                scoped_prompt,
                cooldown=cooldown,
            )

        factuality.route_text_audit = route_with_final_mistral
        try:
            return factuality.audit_plan(
                api_key,
                plan,
                research_context,
                model,
                diagnostics=diagnostics,
            )
        finally:
            factuality.route_text_audit = original_route


def _validate_tone_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(
            result,
            required_arrays=(
                "preachiness_flags",
                "cultural_dignity_flags",
                "naturalness_flags",
                "narrative_format_flags",
                "unverified_religious_quote_flags",
                "notes",
            ),
        )
    except Exception as exc:
        raise MistralExecutorWireFailure(
            f"mistral invalid json tone audit contract {type(exc).__name__.lower()}"
        ) from exc
    return result


def _mistral_tone_call(prompt: str) -> dict[str, Any]:
    return _validate_tone_result(
        mistral_executor_json(
            prompt,
            max_tokens=2600,
            task_kind="text_audit",
            response_schema=("clean_v2_tone_naturalness_audit_v1", TONE_AUDIT_SCHEMA),
            temperature=0.1,
        )
    )


def audit_tone_with_mistral(
    api_key: str,
    plan: object,
    model: str,
) -> dict[str, Any]:
    """Run the frozen Engine tone/naturalness audit with Mistral as the final strict-schema route."""
    from isco_video_agent import text_audit_router
    from isco_video_agent import tone_quality

    with _AUDIT_ROUTE_LOCK:
        original_route = tone_quality.route_text_audit

        def route_with_final_mistral(
            providers,
            prompt: str,
            *,
            cooldown: set[str] | None = None,
        ):
            names = tuple(str(name) for name, _call in providers)
            if names != _EXPECTED_BASE_ROUTE:
                raise RuntimeError(
                    "Clean V2 tone audit base provider route drift: " + "->".join(names)
                )

            def contract_validated(call):
                def invoke(value: str):
                    return _validate_tone_result(call(value))

                return invoke

            extended = [
                (name, contract_validated(call)) for name, call in providers
            ]
            extended.append(("mistral", _mistral_tone_call))
            return text_audit_router.route_text_audit(
                extended,
                prompt,
                cooldown=cooldown,
            )

        tone_quality.route_text_audit = route_with_final_mistral
        try:
            return tone_quality.audit_tone_and_naturalness(
                api_key,
                plan,
                model,
            )
        finally:
            tone_quality.route_text_audit = original_route
