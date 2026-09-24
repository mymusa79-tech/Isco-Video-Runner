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
        "hook_specificity": {"type": "boolean"},
        "hook_honesty": {"type": "boolean"},
        "hook_curiosity": {"type": "boolean"},
        "hook_genericness": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "status",
        "preachiness_flags",
        "cultural_dignity_flags",
        "naturalness_flags",
        "narrative_format_flags",
        "unverified_religious_quote_flags",
        "hook_specificity",
        "hook_honesty",
        "hook_curiosity",
        "hook_genericness",
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
_HOOK_QUALITY_FIELDS = (
    "hook_specificity",
    "hook_honesty",
    "hook_curiosity",
    "hook_genericness",
)


_LEGACY_RELIGIOUS_QUOTE_RULE = (
    "5. Unverified religious quotations: flag any religious quotation or attribution presented as authoritative unless the\n"
    "   approved research context directly supports it as verified. Judge this semantically - do not rely only on a fixed\n"
    "   list of marker phrases."
)
_RELIGIOUS_QUOTE_SCOPE_CLARIFICATION = (
    "\n   Scope clarification for Clean V2: ordinary non-quoted invocations or greetings such as "
    "بسم الله / باسم الله / حفظكم الله are not quotations or attributions by themselves. "
    "Do not flag them under unverified_religious_quote_flags unless they actually quote or attribute "
    "religious authority/content that requires source verification."
)


def _scope_religious_quote_prompt(prompt: str) -> str:
    """Clarify quotation scope without weakening the legacy verified-source rule."""
    if _LEGACY_RELIGIOUS_QUOTE_RULE not in prompt:
        raise RuntimeError("Clean V2 tone religious-quote rule drift")
    return prompt.replace(
        _LEGACY_RELIGIOUS_QUOTE_RULE,
        _LEGACY_RELIGIOUS_QUOTE_RULE + _RELIGIOUS_QUOTE_SCOPE_CLARIFICATION,
        1,
    )


def _scope_clean_v2_tone_prompt(prompt: str) -> str:
    """Keep the legacy semantic audit focused on narration the repair can own."""
    scoped = _scope_religious_quote_prompt(prompt)
    return scoped + """
[CLEAN_V2_TONE_SCOPE]
- This is a spoken-text audit. Do not block on visual_query, footage choice, shot choice,
  visual metaphor, or visual cohesion; those belong to the later Visual QA stage.
- The contextual CTA anchor section is host-owned. Do not require moving the CTA to a
  different section or to the ending. Judge only whether the exact CTA is integrated
  naturally inside its existing anchor section.
- The narrative identity opener/closer are host-owned exact phrases. Do not request
  rewriting them; judge only the surrounding spoken transition.
- Evaluate the actual PLAN hook (the first spoken sentence) with four required booleans in the
  SAME audit response; this adds no provider call:
  * hook_specificity=true only when the hook names a concrete situation, tension, behavior,
    consequence, or question rather than a broad motivational claim.
  * hook_honesty=true only when it sounds believable and natural, not manufactured, inflated,
    manipulative, or written as forced shock/clickbait.
  * hook_curiosity=true only when it creates a genuine reason to hear the next sentence by opening
    a specific unresolved question/tension; calm hooks are fully acceptable.
  * hook_genericness=true when changing roughly one or two words could make the same sentence fit
    dozens of unrelated videos. Genericness=true is always a defect.
- A hook passes only when specificity, honesty, and curiosity are true AND genericness is false.
  If it fails, set status=block and add one concise narrative_format_flags item prefixed exactly
  "hook_quality:" naming the failed dimension(s). Do not demand sensationalism.
- Extend the existing JSON object with exactly these required boolean fields:
  "hook_specificity", "hook_honesty", "hook_curiosity", "hook_genericness".
[/CLEAN_V2_TONE_SCOPE]
""".strip()


def _validate_tone_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(result, required_arrays=_REQUIRED_ARRAYS)
        wrong_hook_fields = [
            field
            for field in _HOOK_QUALITY_FIELDS
            if field not in result or type(result[field]) is not bool
        ]
        if wrong_hook_fields:
            raise ValueError(
                "tone audit response missing/invalid hook boolean(s): "
                + ", ".join(wrong_hook_fields)
            )

        failed: list[str] = []
        if not result["hook_specificity"]:
            failed.append("hook_specificity")
        if not result["hook_honesty"]:
            failed.append("hook_honesty")
        if not result["hook_curiosity"]:
            failed.append("hook_curiosity")
        if result["hook_genericness"]:
            failed.append("hook_genericness")

        if failed:
            result["status"] = "block"
            flags = result["narrative_format_flags"]
            if not any(str(item).startswith("hook_quality:") for item in flags):
                flags.append("hook_quality: failed " + ", ".join(failed))
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
            response_schema=("clean_v2_tone_naturalness_audit_v2", TONE_AUDIT_SCHEMA),
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

            scoped_prompt = _scope_clean_v2_tone_prompt(prompt)
            extended = [
                (name, contract_validated(call)) for name, call in providers
            ]
            extended.append(("mistral", _mistral_tone_call))
            return text_audit_router.route_text_audit(
                extended,
                scoped_prompt,
                cooldown=cooldown,
            )

        tone_quality.route_text_audit = route_with_final_mistral
        try:
            result = tone_quality.audit_tone_and_naturalness(api_key, plan, model)
            raw = result.get("raw_result")
            if isinstance(raw, dict):
                for field in _HOOK_QUALITY_FIELDS:
                    if type(raw.get(field)) is bool:
                        result[field] = raw[field]
            return result
        finally:
            tone_quality.route_text_audit = original_route
