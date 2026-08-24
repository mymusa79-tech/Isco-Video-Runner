from __future__ import annotations

import copy
import json
import re
import unicodedata
from difflib import SequenceMatcher
from functools import wraps

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged
import scripts.append_retry_guard as append_retry_guard


_SENTENCE_SPLIT = re.compile(r"(?<=[.!؟!])\s+")
_NEAR_ANCHOR_THRESHOLD = 0.84


def _normalize_arabic(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^\w\s\u0600-\u06FF]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip().lower()
    return value


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in _SENTENCE_SPLIT.split(str(text).strip()) if part.strip()]


def _near_anchor(sentence: str, anchor_sentence: str) -> bool:
    left = _normalize_arabic(sentence)
    right = _normalize_arabic(anchor_sentence)
    if not left or not right:
        return False
    if left == right:
        return True
    return SequenceMatcher(None, left, right).ratio() >= _NEAR_ANCHOR_THRESHOLD


def _strip_anchor_like_sentences(text: str, anchor: str) -> str:
    anchor_sentences = _sentences(anchor)
    if not anchor_sentences:
        return str(text).strip()
    kept: list[str] = []
    for sentence in _sentences(text):
        if any(_near_anchor(sentence, anchor_sentence) for anchor_sentence in anchor_sentences):
            continue
        kept.append(sentence)
    return " ".join(kept).strip()


def _redact_writer_policy_json(policy_json: str) -> str:
    """Remove literal identity anchors before any Runner-owned narration repair call.

    Engine Run #88 isolation owns this natively once the matching Engine revision is
    pinned, but Runner's post-build residual repair reloads editorial policy on its own.
    Keep this compatibility guard so that override path cannot re-expose canonical
    opener/closer text to a writer/repair model.
    """
    try:
        policy = json.loads(policy_json)
    except Exception:
        return policy_json
    if not isinstance(policy, dict):
        return policy_json

    engine_redactor = getattr(staged, "_writer_policy_json", None)
    if callable(engine_redactor):
        return engine_redactor(policy)

    redacted = dict(policy)
    signature = policy.get("brand_signature")
    if isinstance(signature, dict):
        safe_signature = {
            key: value
            for key, value in signature.items()
            if key not in {"opener", "closer"}
        }
        safe_signature["host_managed"] = True
        safe_signature["runtime_rule"] = (
            "The host injects exactly one identity opener and exactly one identity closer. "
            "Narration repairers must not write, quote, paraphrase, imitate, or invent "
            "a channel-specific opener, farewell, sign-off, outro, or second ending."
        )
        redacted["brand_signature"] = safe_signature
    return json.dumps(redacted, ensure_ascii=False)


def _mask_terminal_closer_for_writer(sections: list, closer: str) -> list:
    """Hide the literal runtime closer from repair prompts without changing counts.

    The returned list contains shallow copies only where needed. The real plan remains
    untouched. Replacing each closer word with an ellipsis token preserves the Engine's
    whitespace word-count arithmetic, so append bounds remain identical while the model
    cannot echo or paraphrase the channel sign-off from prompt context.
    """
    if not sections:
        return sections
    closer = str(closer or "").strip()
    if not closer:
        return sections

    masked = list(sections)
    last = copy.copy(masked[-1])
    narration = str(last.narration)
    if closer in narration:
        token_count = max(1, staged._word_count(closer))
        mask = " ".join(["…"] * token_count)
        last.narration = narration.replace(closer, mask)
        masked[-1] = last
    return masked


def _install_append_retry_identity_isolation() -> None:
    """Protect both Engine-internal and Runner post-build append-repair prompts."""
    current = append_retry_guard._repair_all_residual_underlength
    if getattr(current, "_isco_run88_identity_isolation", False):
        if staged._script_doctor_underlength_retry is not current:
            staged._script_doctor_underlength_retry = current
        return

    original = current

    def guarded_repair(
        api_key: str,
        *,
        topic: str,
        model: str,
        sections: list,
        policy_json: str,
        research_json: str,
        narrative_format: str,
        current_words: int,
        minimum: int,
        editorial_intent_json: str = "",
    ) -> dict[str, str]:
        closer = append_retry_guard._ACTIVE_CLOSER.get()
        safe_sections = _mask_terminal_closer_for_writer(sections, closer or "")
        safe_policy_json = _redact_writer_policy_json(policy_json)
        return original(
            api_key,
            topic=topic,
            model=model,
            sections=safe_sections,
            policy_json=safe_policy_json,
            research_json=research_json,
            narrative_format=narrative_format,
            current_words=current_words,
            minimum=minimum,
            editorial_intent_json=editorial_intent_json,
        )

    guarded_repair._isco_run88_identity_isolation = True
    append_retry_guard._repair_all_residual_underlength = guarded_repair
    if staged._script_doctor_underlength_retry is original:
        staged._script_doctor_underlength_retry = guarded_repair


def _enforce_brand_anchors_once(plan):
    if getattr(plan, "format", "") == "moment" or not getattr(plan, "sections", None):
        return plan

    opener = str(getattr(plan, "identity_opener", "") or "").strip()
    closer = str(getattr(plan, "identity_closer", "") or "").strip()
    # Run #80: this guard runs after every word-band/floor check in the build_plan
    # chain has already passed, with nothing downstream to re-verify it. Stripping
    # a near-duplicate anchor sentence removes the WHOLE matching sentence, not
    # just the literal anchor phrase, and can legitimately remove enough words to
    # push the opening/closing section back under the hard Film 110-word floor
    # (observed: section_1 102 words, section_8 106 words after this ran on a
    # freshly-regenerated quality-review repair). Only apply the strip when the
    # result still clears the floor; otherwise keep the section exactly as it
    # was handed to this guard rather than silently reintroducing an under-floor
    # result this late in the pipeline.
    film_floor = staged._FILM_SECTION_MIN_WORDS if getattr(plan, "format", None) == "film" else None

    first = plan.sections[0]
    last = plan.sections[-1]

    def _strip_would_regress_below_floor(candidate: str, original: str) -> bool:
        if film_floor is None:
            return False
        # Only block the strip when it would newly break an already-compliant
        # section; a section that was already under floor before this guard ran
        # has an existing problem outside this guard's scope and stripping still
        # proceeds, matching prior behavior.
        return staged._word_count(original) >= film_floor > staged._word_count(candidate)

    if opener:
        cleaned_first = _strip_anchor_like_sentences(first.narration, opener)
        candidate = staged._insert_after_first_sentence(cleaned_first, opener)
        if not _strip_would_regress_below_floor(candidate, first.narration):
            first.narration = candidate

    if closer:
        cleaned_last = _strip_anchor_like_sentences(last.narration, closer)
        candidate = f"{cleaned_last.rstrip()} {closer}".strip()
        if not _strip_would_regress_below_floor(candidate, last.narration):
            last.narration = candidate

    return plan


def install_brand_anchor_guard() -> None:
    """Sanitize only channel identity anchors after each completed plan build.

    Script Doctor may lightly rewrite an already-present opener/closer and the
    Engine then restores the exact anchor. That can create near-duplicate loops.
    This wrapper removes only sentences strongly similar to the declared identity
    anchor and then restores the exact anchor once. It does not alter middle
    sections, provider calls, audits, or duration gates.
    """
    _install_append_retry_identity_isolation()

    current = orchestrator.build_plan
    if getattr(current, "_isco_brand_anchor_guard", False):
        print("Brand anchor guard already installed")
        return

    @wraps(current)
    def guarded_build_plan(*args, **kwargs):
        plan = current(*args, **kwargs)
        return _enforce_brand_anchors_once(plan)

    guarded_build_plan._isco_brand_anchor_guard = True
    orchestrator.build_plan = guarded_build_plan
    # Do not rebind staged.build_plan here. install_router() deliberately delegates
    # routed_build_plan() through that module-level callable; rebinding it to this
    # outer wrapper makes routed_build_plan -> staged.build_plan -> routed_build_plan
    # recurse before the first provider attempt.
    print(
        "Brand anchor guard installed: opener/closer normalized to one exact anchor each; "
        "Runner residual-repair identity context isolated"
    )
