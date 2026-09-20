from __future__ import annotations

"""Clean V2 tone/naturalness audit bridge with strict Mistral fallback."""

import threading
from typing import Any

from .mistral_executor import MistralExecutorWireFailure, mistral_executor_json


TONE_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["pass", "block"]},
        "preachiness_flags": {"type": "array", "items": {"type": "string"}},
        "cultural_dignity_flags": {"type": "array", "items": {"type": "string"}},
        "naturalness_flags": {"type": "array", "items": {"type": "string"}},
        "narrative_format_flags": {"type": "array", "items": {"type": "string"}},
        "unverified_religious_quote_flags": {"type": "array", "items": {"type": "string"}},
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
_REQUIRED_ARRAYS = (
    "preachiness_flags",
    "cultural_dignity_flags",
    "naturalness_flags",
    "narrative_format_flags",
    "unverified_religious_quote_flags",
    "notes",
)
_AUDIT_ROUTE_LOCK = threading.RLock()


def _validate_tone_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(result, required_arrays=_REQUIRED_ARRAYS)
    except Exception as exc:
        raise MistralExecutorWireFailure(
            f"tone audit invalid contract {type(exc).__name__.lower()}"
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


def audit_tone_and_naturalness_with_mistral(
    api_key: str,
    plan: object,
    model: str,
) -> dict[str, Any]:
    """Reuse the frozen Engine audit and append one strict-schema Mistral route."""
    from isco_video_agent import text_audit_router, tone_quality

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
            return tone_quality.audit_tone_and_naturalness(api_key, plan, model)
        finally:
            tone_quality.route_text_audit = original_route
