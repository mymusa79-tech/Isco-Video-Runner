from __future__ import annotations

"""Clean V2 factuality route with one structured location contract for every provider."""

import json
import threading
from typing import Any

from .mistral_executor import MistralExecutorWireFailure, mistral_executor_json


_EXPECTED_BASE_ROUTE = ("gemini", "groq", "openrouter")
_AUDIT_ROUTE_LOCK = threading.RLock()
_FLAG_FIELDS = (
    "unsupported_claims",
    "professional_advice_flags",
    "expert_persona_flags",
)

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


def _section_ids(plan: object) -> tuple[str, ...]:
    sections = list(getattr(plan, "sections", ()) or ())
    ids = tuple(str(getattr(item, "id", "") or "").strip() for item in sections)
    if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("Clean V2 factuality requires unique non-empty section ids")
    return ids


def _text_audit_schema(section_ids: tuple[str, ...]) -> dict[str, Any]:
    issue = {
        "type": "object",
        "properties": {
            "section_id": {"type": "string", "enum": list(section_ids)},
            "issue": {"type": "string", "minLength": 1},
        },
        "required": ["section_id", "issue"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["pass", "block"]},
            "unsupported_claims": {"type": "array", "items": issue},
            "professional_advice_flags": {"type": "array", "items": issue},
            "expert_persona_flags": {"type": "array", "items": issue},
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


# Kept as a module-level compatibility shape for imports/tests. Production always
# builds the authoritative enum dynamically from the current plan.
TEXT_AUDIT_SCHEMA: dict[str, Any] = _text_audit_schema(("s1", "s2", "s3"))


def _scope_professional_advice_prompt(prompt: str) -> str:
    if _LEGACY_PROFESSIONAL_ADVICE_RULE not in prompt:
        raise RuntimeError("Clean V2 factuality professional-advice rule drift")
    return prompt.replace(
        _LEGACY_PROFESSIONAL_ADVICE_RULE,
        _LEGACY_PROFESSIONAL_ADVICE_RULE + _PRODUCTIVITY_SCOPE_CLARIFICATION,
        1,
    )


def _structured_location_prompt(prompt: str, section_ids: tuple[str, ...]) -> str:
    contract = {
        "status": "pass or block",
        "unsupported_claims": [{"section_id": section_ids[0], "issue": "short description"}],
        "professional_advice_flags": [{"section_id": section_ids[0], "issue": "short description"}],
        "expert_persona_flags": [{"section_id": section_ids[0], "issue": "short description"}],
        "notes": ["short notes"],
    }
    return (
        _scope_professional_advice_prompt(prompt)
        + "\n\nCLEAN_V2 STRUCTURED LOCATION CONTRACT (authoritative output shape): "
        + "Every item in unsupported_claims, professional_advice_flags, and expert_persona_flags "
        + "MUST be an object with exactly section_id and issue. section_id MUST be one of "
        + json.dumps(list(section_ids), ensure_ascii=False)
        + ". Never encode the section location inside issue text. Empty flag arrays are allowed. "
        + "Return exactly this structural shape: "
        + json.dumps(contract, ensure_ascii=False)
    )


def _validate_factuality_result(
    result: dict[str, Any], section_ids: tuple[str, ...]
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("invalid json factuality contract: response must be object")
    if result.get("status") not in {"pass", "block"}:
        raise ValueError("invalid json factuality contract: invalid status")
    valid_ids = set(section_ids)
    for field in (*_FLAG_FIELDS, "notes"):
        if field not in result or not isinstance(result[field], list):
            raise ValueError(f"invalid json factuality contract: {field} must be array")
    for field in _FLAG_FIELDS:
        for item in result[field]:
            if not isinstance(item, dict) or set(item) != {"section_id", "issue"}:
                raise ValueError(f"invalid json factuality contract: {field} item shape")
            if str(item.get("section_id") or "") not in valid_ids:
                raise ValueError(f"invalid json factuality contract: {field} section_id")
            if not str(item.get("issue") or "").strip():
                raise ValueError(f"invalid json factuality contract: {field} issue")
    if any(not isinstance(item, str) for item in result["notes"]):
        raise ValueError("invalid json factuality contract: notes item")
    return result


def _mistral_factuality_call(
    prompt: str, *, schema: dict[str, Any], section_ids: tuple[str, ...]
) -> dict[str, Any]:
    return _validate_factuality_result(
        mistral_executor_json(
            prompt,
            max_tokens=2200,
            task_kind="text_audit",
            response_schema=("clean_v2_factuality_audit_v2", schema),
            temperature=0.1,
        ),
        section_ids,
    )


def audit_plan_with_mistral(
    api_key: str,
    plan: object,
    research_context: object,
    model: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the frozen Engine semantics with a shared strict location schema on all legs."""
    del api_key, model
    from isco_video_agent import factuality
    from isco_video_agent import text_audit_router
    from clean_v2 import providers as clean_providers

    section_ids = _section_ids(plan)
    schema = _text_audit_schema(section_ids)

    with _AUDIT_ROUTE_LOCK:
        original_route = factuality.route_text_audit

        def route_with_final_mistral(providers, prompt: str, *, cooldown: set[str] | None = None):
            names = tuple(str(name) for name, _call in providers)
            if names != _EXPECTED_BASE_ROUTE:
                raise RuntimeError("Clean V2 factuality base provider route drift: " + "->".join(names))
            scoped_prompt = _structured_location_prompt(prompt, section_ids)

            def gemini_call(value: str) -> dict[str, Any]:
                return _validate_factuality_result(
                    clean_providers._gemini_call(value, 2200, response_schema=schema),
                    section_ids,
                )

            def groq_call(value: str) -> dict[str, Any]:
                return _validate_factuality_result(
                    clean_providers._groq_call(
                        value, 2200, response_schema=schema,
                        schema_name="clean_v2_factuality_audit_v2",
                    ),
                    section_ids,
                )

            def openrouter_call(value: str) -> dict[str, Any]:
                return _validate_factuality_result(
                    clean_providers._openrouter_call(
                        value, 2200, response_schema=schema,
                        schema_name="clean_v2_factuality_audit_v2",
                    ),
                    section_ids,
                )

            extended = [
                ("gemini", gemini_call),
                ("groq", groq_call),
                ("openrouter", openrouter_call),
                ("mistral", lambda value: _mistral_factuality_call(
                    value, schema=schema, section_ids=section_ids
                )),
            ]
            return text_audit_router.route_text_audit(
                extended, scoped_prompt, cooldown=cooldown
            )

        factuality.route_text_audit = route_with_final_mistral
        try:
            return factuality.audit_plan(
                "", plan, research_context, "", diagnostics=diagnostics
            )
        finally:
            factuality.route_text_audit = original_route
