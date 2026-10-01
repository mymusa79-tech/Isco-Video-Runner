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
        "hook_body_continuity": {"type": "boolean"},
        "payoff_resolves_hook": {"type": "boolean"},
        "section_dependency": {"type": "boolean"},
        "topic_fidelity": {"type": "boolean"},
        "notes": {"type": "array", "items": {"type": "string"}},
        "filler_flags": {"type": "array", "items": {"type": "string"}},
        "payoff_earned": {"type": "boolean"},
        "cold_open_story_violation": {"type": "boolean"},
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
        "hook_body_continuity",
        "payoff_resolves_hook",
        "section_dependency",
        "topic_fidelity",
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
    "hook_body_continuity",
    "payoff_resolves_hook",
)
_CONTENT_DEPENDENCY_FIELDS = ("section_dependency", "topic_fidelity")


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
- SPOKEN ARABIC SURFACE CHECK — inside this SAME audit call and before the final verdict, scan
  every authored narration sentence, not only the hook. Block clear Arabic grammar or sentence-
  completeness defects that would sound wrong aloud: demonstrative/noun agreement (for example
  «هذا التوقعات»), pronoun/reference agreement, broken conjunctions, or a dependent fragment
  such as a section beginning with «مما ...» without a grammatical antecedent in that sentence.
  For every such defect, add one naturalness_flags item that includes the affected section id
  (s1/s2/...) and a short exact excerpt from the draft. Do not flag stylistic preference as grammar.
- Evaluate the actual PLAN hook (the first spoken sentence) with six required booleans in the
  SAME audit response; this adds no provider call:
  * hook_specificity=true only when the hook names a concrete situation, tension, behavior,
    consequence, or question rather than a broad motivational claim.
  * hook_honesty=true only when it sounds believable and natural, not manufactured, inflated,
    manipulative, or written as forced shock/clickbait.
  * hook_curiosity=true only when it creates a genuine reason to hear the next sentence by opening
    a specific unresolved question/tension; calm hooks are fully acceptable.
  * hook_genericness=true when changing roughly one or two words could make the same sentence fit
    dozens of unrelated videos. Genericness=true is always a defect.
  * hook_body_continuity=true only when the body keeps developing the SAME unresolved tension after
    the host-owned prayer/channel handoff, with each section adding meaning rather than restarting,
    repeating, or dropping into generic advice.
  * payoff_resolves_hook=true only when the actual closing payoff directly and satisfactorily
    answers the SAME question/tension opened by the actual first spoken sentence. Topic similarity
    alone is insufficient.
- A hook-to-payoff spine passes only when specificity, honesty, curiosity, hook_body_continuity,
  and payoff_resolves_hook are true AND genericness is false.
  If it fails, set status=block and add one concise narrative_format_flags item prefixed exactly
  "hook_quality:" naming the failed dimension(s). Do not demand sensationalism.
- CONTENT DEPTH applies to Short, Film, and Podcast inside this SAME audit call. Block shallow narration
  when a body section merely paraphrases the prior section, relies on broad motivational language that
  could fit unrelated topics, gives generic advice before explaining the episode-specific tension, or
  reaches a payoff that does not depend on the reasoning built before it. Do NOT demand new facts,
  studies, statistics, diagnoses, or unsupported mechanisms; depth means clearer reasoning from the
  already approved material, not more factual claims. For Short specifically, inspect the FINAL s3
  practical-action sentence as part of the same semantic spine: it must directly operationalize the
  exact hook/payoff tension. A generic action that could close an unrelated productivity, procrastination,
  confidence, or motivation video is a content_depth:s3 defect even when its Arabic is grammatical.
  Also block topic drift where the payoff suddenly switches mechanisms (for example from comparison to
  friction/procrastination) merely because the closing sentence sounds useful in isolation. For each
  concrete defect add one concise narrative_format_flags item prefixed exactly "content_depth:" and
  include the affected section id (for example content_depth:s2 ...). Set status=block when any such defect exists.
- DEPENDENCY / TOPIC-FIDELITY TEST — still inside this SAME audit call:
  * section_dependency=true only when every non-identity section adds a distinct piece of reasoning whose
    position matters. Set it false if a section can be removed or swapped without weakening the explanation,
    or if it jumps from the episode's tension into generic advice.
  * topic_fidelity=true only when every explanatory or prescriptive sentence is earned by THIS episode's
    central tension and preceding reasoning. Set it false when a sentence introduces a generic mechanism or
    slogan that could close many unrelated videos. Example pattern to reject: saying that a "small step reduces
    friction" in an episode about social comparison when friction was never established or explained.
  * When either boolean is false, set status=block and add a concise narrative_format_flags item prefixed
    "content_dependency:" with the affected section id and exact short excerpt.
- Extend the existing JSON object with these two required booleans:
  "section_dependency", "topic_fidelity".
- The JSON object must include these required hook booleans:
  "hook_specificity", "hook_honesty", "hook_curiosity", "hook_genericness",
  "hook_body_continuity", "payoff_resolves_hook".
- EDITORIAL_VOICE_ADVISORY (observation only - this never changes status and never
  blocks production; a missing or malformed value here is simply treated as "no
  issue found", never as a rejection):
  * filler_flags: list each sentence that only restates a point already made, marks
    time without adding new information, or could be deleted without losing meaning
    (hollow transitions such as "لكن الحقيقة أن", "في الواقع", or a restated setup).
    Empty array when none are found.
  * payoff_earned=true only when the closing payoff/resolution depends on a specific
    concrete detail already established earlier in THIS narration; false when it is
    a generic statement that could just as easily close many unrelated topics.
  * cold_open_story_violation is only meaningful when the narration's own shape is a
    scene, inner voice, or narrative turn - NOT an explicit listicle, direct Q&A, or
    two-speaker dialogue format. For those explicitly structured formats always set
    it to false. Otherwise, true only when the opening line is a general statement,
    address, or instruction rather than landing inside a concrete moment, sensation,
    or action already under way.
  * Add these three fields to the SAME JSON object: "filler_flags" (array of
    strings), "payoff_earned" (boolean), "cold_open_story_violation" (boolean).
[/CLEAN_V2_TONE_SCOPE]
""".strip()


def _enforce_hook_quality_contract(result: dict[str, Any]) -> dict[str, Any]:
    """Apply the hook verdict locally after the existing semantic audit."""
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
    if not result["hook_body_continuity"]:
        failed.append("hook_body_continuity")
    if not result["payoff_resolves_hook"]:
        failed.append("payoff_resolves_hook")

    if failed:
        result["status"] = "block"
        flags = result.get("narrative_format_flags")
        if not isinstance(flags, list):
            raise ValueError("tone audit narrative_format_flags must be an array")
        if not any(str(item).startswith("hook_quality:") for item in flags):
            flags.append("hook_quality: failed " + ", ".join(failed))
    return result


def _enforce_content_dependency_contract(result: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when the same audit detects generic or swappable reasoning."""
    wrong = [
        field
        for field in _CONTENT_DEPENDENCY_FIELDS
        if field not in result or type(result[field]) is not bool
    ]
    if wrong:
        raise ValueError(
            "tone audit response missing/invalid content boolean(s): "
            + ", ".join(wrong)
        )
    failed: list[str] = []
    if not result["section_dependency"]:
        failed.append("section_dependency")
    if not result["topic_fidelity"]:
        failed.append("topic_fidelity")
    if failed:
        result["status"] = "block"
        flags = result.get("narrative_format_flags")
        if not isinstance(flags, list):
            raise ValueError("tone audit narrative_format_flags must be an array")
        if not any(str(item).startswith("content_dependency:") for item in flags):
            flags.append("content_dependency: failed " + ", ".join(failed))
    return result


def _normalize_editorial_voice_advisory(result: dict[str, Any]) -> dict[str, Any]:
    """Coerce the observation-only editorial-voice fields to safe defaults.

    Advisory only, by design (not yet a blocking gate): never raises, never
    sets status=block. A missing or malformed value from a weak fallback
    provider defaults to "no issue found" rather than rejecting the whole
    audit response over an optional signal - the opposite failure mode would
    turn a purely observational addition into a new way for an already
    quota-strained provider chain to fail closed.
    """
    filler_flags = result.get("filler_flags")
    result["filler_flags"] = (
        [str(item) for item in filler_flags] if isinstance(filler_flags, list) else []
    )
    payoff_earned = result.get("payoff_earned")
    result["payoff_earned"] = payoff_earned if isinstance(payoff_earned, bool) else True
    cold_open_violation = result.get("cold_open_story_violation")
    result["cold_open_story_violation"] = (
        cold_open_violation if isinstance(cold_open_violation, bool) else False
    )
    return result


def _validate_tone_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(result, required_arrays=_REQUIRED_ARRAYS)
        result = _enforce_hook_quality_contract(result)
        result = _enforce_content_dependency_contract(result)
        return _normalize_editorial_voice_advisory(result)
    except Exception as exc:
        raise MistralExecutorWireFailure(
            f"tone audit invalid contract {type(exc).__name__.lower()}"
        ) from exc


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
                if isinstance(raw.get("filler_flags"), list):
                    result["filler_flags"] = raw["filler_flags"]
                if type(raw.get("payoff_earned")) is bool:
                    result["payoff_earned"] = raw["payoff_earned"]
                for field in _CONTENT_DEPENDENCY_FIELDS:
                    if type(raw.get(field)) is bool:
                        result[field] = raw[field]
                if type(raw.get("cold_open_story_violation")) is bool:
                    result["cold_open_story_violation"] = raw["cold_open_story_violation"]
            return _normalize_editorial_voice_advisory(result)
        finally:
            tone_quality.route_text_audit = original_route
