from __future__ import annotations

"""Clean V2 tone/naturalness audit bridge with strict Mistral fallback."""

import re
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

# Groq's strict decoder requires every declared property on the wire. These
# advisory fields remain non-blocking in the semantic validator and retain its
# safe defaults for legacy responses; only the new HTTP schema is tightened.
TONE_AUDIT_HTTP_SCHEMA = {
    **TONE_AUDIT_SCHEMA,
    "required": list(TONE_AUDIT_SCHEMA["properties"]),
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

_LEGACY_FRAGMENT_RULE = (
    "   Pay special attention to the closing/payoff line (s3/final section): flag any sentence that opens with a\n"
    "   conjunction (e.g. \"و\") directly attached to a verbal noun/gerund (masdar) with no independent verb anywhere in the\n"
    "   sentence - e.g. \"وتقليل الخيارات إلى ما هو مهم فعلاً\" is a dangling fragment, not a complete sentence, even though\n"
    "   it reads fluently at a glance. A correct version uses an actual verb or imperative, e.g.\n"
    "   \"فقلّل الخيارات إلى ما هو مهم فعلاً\". This exact failure mode has shipped to production before (Telegram Run #74)\n"
    "   and must not pass silently."
)
_ARABIC_SENTENCE_RULE = (
    "   Inspect sentence completeness, including the actual final section. A complete Arabic nominal sentence\n"
    "   needs a subject and predicate, not a finite verb. For example «وتدوين خطواتك الشخصية هو وسيلة لتقدير تقدمك»\n"
    "   is a complete nominal sentence; assess any separate overclaim on its own merits. A bare masdar phrase such as\n"
    "   «وتقليل الخيارات إلى ما هو مهم فعلاً» without a predicate or completing clause is a real fragment.\n"
    "   Flag only the actual incomplete construction and quote it exactly. Never assume an implied opening «و»\n"
    "   or another word absent from the draft. Keep real grammar, agreement and completeness defects blocking."
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


def _scope_clean_v2_tone_prompt(
    prompt: str,
    *,
    research_boundaries: str = "",
    editorial_shape_context: str = "",
    repair_verification_context: str = "",
) -> str:
    """Keep the semantic judge strict while respecting the approved evidence ceiling."""
    scoped = _scope_religious_quote_prompt(prompt).replace(
        _LEGACY_FRAGMENT_RULE, _ARABIC_SENTENCE_RULE, 1
    )
    evidence_scope = ""
    if research_boundaries.strip():
        evidence_scope = """
[CLEAN_V2_EVIDENCE_BOUNDARY]
The attached RESEARCH_BOUNDARIES are hard evidence ceilings, not optional context.
- Keep the same strict hook/progression/depth bar, but judge depth only from claims and reasoning the
  approved evidence can support.
- If a boundary explicitly says it does NOT establish causality, diagnosis, percentages, or scientific
  mechanism, do NOT block merely because the draft lacks such a mechanism and do NOT recommend inventing
  one. Demand specificity through observable behavior, a concrete choice pattern, a supported distinction,
  a consequence already present in approved material, or a clearly framed non-causal interpretation.
- Never suggest adding studies, psychological triggers, algorithmic motives, hidden mental processes, or
  stronger causal explanations outside the boundary. A text can be deep without pretending evidence exists.
[/CLEAN_V2_EVIDENCE_BOUNDARY]
""" + research_boundaries.strip()
    return scoped + """
[CLEAN_V2_TONE_SCOPE]
- This is a spoken-text audit. Do not block on visual_query, footage choice, shot choice,
  visual metaphor, or visual cohesion; those belong to the later Visual QA stage.
- The contextual CTA anchor section is host-owned. Do not require moving the CTA to a
  different section or to the ending. Judge only whether the exact CTA is integrated
  naturally inside its existing anchor section.
- The narrative identity opener/closer are host-owned exact phrases. Do not request
  rewriting them; judge only the surrounding spoken transition.
- ONE-PASS COMPLETE DEFECT INVENTORY — before the FIRST verdict, examine the opening hook, EVERY actual
  body section, and the actual final payoff independently; for Short also inspect the final practical action. Do not stop after
  spotting one grammar defect: report every independently supported blocking flaw at once, with
  its correct section id and stable marker (content_depth:s2, content_depth:s3
  practical_action_generic:, hook_quality:, or content_dependency:s2/s3 as applicable). The
  existing single bounded repair must be able to fix all cited s2/s3 defects together; do not
  invent defects merely to fill these categories. Do not add a second audit or provider call.
- SPOKEN ARABIC SURFACE CHECK — inside this SAME audit call and before the final verdict, scan
  every authored narration sentence, not only the hook. Block clear Arabic grammar or sentence-
  completeness defects that would sound wrong aloud: demonstrative/noun agreement (for example
  «هذا التوقعات»), pronoun/reference agreement, broken conjunctions, or a dependent fragment
  such as a section beginning with «مما ...» without a grammatical antecedent in that sentence.
  Do not misclassify a finite imperative clause as a masdar fragment: forms such as «فأدخل ...»،
  «فاكتب ...»، «فاختر ...»، «فقارن ...»، «فتوقف ...»، and «فقلّل ...» are verb-led clauses when
  they have their required object/complement. Likewise, a full main clause such as
  «تتحرر من الضغط حين تدرك السبب» is grammatical; do not call it a dangling
  «حين» fragment merely because it contains a subordinate clause. Judge the actual Arabic syntax,
  not the surface prefix.
  A complete Arabic nominal sentence can have a subject and predicate without a finite verb.
  Do not invent an implied conjunction, verb or prefix absent from the quoted draft.
  For every such defect, add one naturalness_flags item that includes the affected section id
  (s1/s2/...) and a short exact excerpt from the draft. Do not flag stylistic preference as grammar.
  Never emit a correction whose proposed replacement is textually identical to the quoted original
  (for example: 'نحن نظن' should be 'نحن نظن'); that is not a defect and must not be a flag.
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
  When THE PRACTICAL ACTION ITSELF is the generic defect, use the stable prefix exactly
  "content_depth:s3 practical_action_generic:" and then a concise explanation; this marker is what allows
  the bounded repair to open that one host-owned field without unlocking other Planning decisions.
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
""".strip() + "\n\n" + _ARABIC_SENTENCE_RULE + (
        "\n\n" + evidence_scope if evidence_scope else ""
    ) + ("\n\n" + editorial_shape_context if editorial_shape_context else "") + (
        "\n\n" + repair_verification_context if repair_verification_context else ""
    )


_ASSERTED_ARABIC_PREFIX = re.compile(
    r"(?:starts? with|starting with|begins? with|beginning with)\s+"
    r"(?:a\s+conjunction\s*)?\(?\s*['\"«](و|حين|عندما|مما|إذا)['\"»]",
    re.I,
)


def _ground_syntax_flags(result: dict[str, Any], sections: dict[str, str]) -> dict[str, Any]:
    """Discard only a provably false claim about an exact excerpt's prefix.

    This is not a grammar checker or a semantic override. Unlocated flags,
    real fragments, other grammar errors and every other quality dimension
    retain the existing blocking behavior.
    """
    flags = result.get("naturalness_flags")
    if not isinstance(flags, list) or not sections:
        return result
    kept, removed = [], []
    for raw in flags:
        flag = str(raw)
        assertion = _ASSERTED_ARABIC_PREFIX.search(flag)
        sid = re.search(r"\bs[1-5]\b", flag)
        text = sections.get(sid.group(0), "") if sid else ""
        quotes = re.findall(r"['\"«]([^'\"»]{8,500})['\"»]", flag)
        surface = re.sub(r"^\s*[AB]:\s*", "", text).lstrip()
        excerpt = next((q for q in quotes if surface.startswith(q)), "")
        if assertion and excerpt:
            actual = re.sub(r"[\u064b-\u065f\u0670]", "", excerpt.lstrip())
            prefix = assertion.group(1)
            starts = (prefix, "و" + prefix, "ف" + prefix) if prefix != "و" else (prefix,)
            if not actual.startswith(starts):
                removed.append(flag)
                continue
        kept.append(raw)
    if not removed:
        return result
    grounded = dict(result)
    grounded["naturalness_flags"] = kept
    grounded["notes"] = list(result.get("notes") or []) + [
        "Discarded false quoted-prefix claim: " + flag for flag in removed
    ]
    if not any(grounded.get(field) for field in _REQUIRED_ARRAYS if field != "notes"):
        grounded["status"] = "pass"
    return grounded


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


_ENGLISH_REPLACEMENT_RE = re.compile(
    r"""[\'\"“«](?P<before>[^\'\"”»]+)[\'\"”»]\s+should\s+be\s+[\'\"“«](?P<after>[^\'\"”»]+)[\'\"”»]""",
    re.IGNORECASE,
)


def _normalize_replacement_text(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _drop_noop_naturalness_replacements(
    result: dict[str, Any],
) -> dict[str, Any]:
    """Discard only self-identical correction flags such as X should be X.

    The Engine intentionally blocks any non-empty naturalness_flags array. Run 59
    exposed a fallback-provider false positive that proposed the exact same Arabic
    phrase as its correction. Filtering happens before the frozen Engine sees the
    validated provider result, so real grammar flags keep the original fail-closed
    behavior unchanged.
    """
    flags = result.get("naturalness_flags")
    if not isinstance(flags, list) or not flags:
        return result

    kept: list[Any] = []
    for raw_flag in flags:
        flag = str(raw_flag)
        match = _ENGLISH_REPLACEMENT_RE.search(flag)
        if match is not None:
            before = _normalize_replacement_text(match.group("before"))
            after = _normalize_replacement_text(match.group("after"))
            if before and before == after:
                continue
        kept.append(raw_flag)

    if len(kept) != len(flags):
        result = dict(result)
        result["naturalness_flags"] = kept
    return result


def _validate_tone_result(result: dict[str, Any]) -> dict[str, Any]:
    from isco_video_agent.text_audit_router import validate_audit_payload

    try:
        validate_audit_payload(result, required_arrays=_REQUIRED_ARRAYS)
        result = _drop_noop_naturalness_replacements(result)
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
    *,
    research_boundaries: str = "",
    preferred_provider: str = "",
    editorial_shape_context: str = "",
    repair_verification_context: str = "",
) -> dict[str, Any]:
    """Keep frozen audit semantics with the bounded Clean V2 HTTP provider route."""
    del api_key, model
    from isco_video_agent import text_audit_router, tone_quality
    from clean_v2 import providers as clean_providers
    authored_sections = {
        str(section.id): str(section.narration)
        for section in getattr(plan, "sections", ())
    }

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

            def gemini_call(value: str) -> dict[str, Any]:
                return _validate_tone_result(
                    _ground_syntax_flags(clean_providers._gemini_call(
                        value, 2600, response_schema=TONE_AUDIT_HTTP_SCHEMA,
                    ), authored_sections)
                )

            def groq_call(value: str) -> dict[str, Any]:
                return _validate_tone_result(
                    _ground_syntax_flags(clean_providers._groq_call(
                        value, 2600, response_schema=TONE_AUDIT_HTTP_SCHEMA,
                        schema_name="clean_v2_tone_naturalness_audit_v2",
                    ), authored_sections)
                )

            def openrouter_call(value: str) -> dict[str, Any]:
                return _validate_tone_result(
                    _ground_syntax_flags(clean_providers._openrouter_call(
                        value, 2600, response_schema=TONE_AUDIT_HTTP_SCHEMA,
                        schema_name="clean_v2_tone_naturalness_audit_v2",
                    ), authored_sections)
                )

            def mistral_call(value: str) -> dict[str, Any]:
                return _validate_tone_result(
                    _ground_syntax_flags(_mistral_tone_call(value), authored_sections)
                )

            scoped_prompt = _scope_clean_v2_tone_prompt(
                prompt,
                research_boundaries=research_boundaries,
                editorial_shape_context=editorial_shape_context,
                repair_verification_context=repair_verification_context,
            )
            extended = [
                ("gemini", gemini_call),
                ("groq", groq_call),
                ("openrouter", openrouter_call),
                ("mistral", mistral_call),
            ]
            # Run 90: the first Tone verdict came from Groq and found one local
            # grammar defect. The repair fixed that defect, but the composite
            # re-audit ran factuality first; its Groq 429 opened the shared
            # circuit, forcing Tone to a different judge (Mistral), which then
            # introduced brand-new semantic objections in unchanged text. For
            # a repair verification, prefer the same judge that authored the
            # original content verdict. This is not approval shopping: a real
            # block from that provider still returns immediately, and technical
            # failure still falls through the unchanged bounded provider mesh.
            preferred = str(preferred_provider or "").strip().lower()
            if preferred:
                match = next(
                    (item for item in extended if item[0] == preferred),
                    None,
                )
                if match is not None:
                    extended = [match] + [
                        item for item in extended if item[0] != preferred
                    ]
            return text_audit_router.route_text_audit(
                extended,
                scoped_prompt,
                cooldown=cooldown,
            )

        tone_quality.route_text_audit = route_with_final_mistral
        try:
            result = tone_quality.audit_tone_and_naturalness("", plan, "")
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

