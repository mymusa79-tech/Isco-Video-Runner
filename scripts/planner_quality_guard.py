from __future__ import annotations

import json
import re
from functools import wraps

import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.resilient_planner as staged


_QUESTION_ANSWER_RUNTIME_RULE = (
    "Question-and-answer structure: raise sincere viewer questions, answer them with layered analysis, never a repetitive FAQ list. "
    "The SPOKEN narration itself must audibly move through natural questions and answers across the episode; do not collapse "
    "question_answer into continuous expository monologue with the format present only in metadata. Questions must deepen the "
    "argument and each answer must advance it."
)

_HUMAN_QUERY_TERMS = {
    "person",
    "people",
    "man",
    "men",
    "woman",
    "women",
    "boy",
    "boys",
    "girl",
    "girls",
    "male",
    "female",
    "couple",
    "adult",
    "teenager",
    "worker",
    "student",
}

_SAFE_FRAMING_TERMS = {
    "silhouette",
    "silhouettes",
    "back",
    "backs",
    "behind",
    "hand",
    "hands",
    "shadow",
    "shadows",
    "faceless",
    "anonymous",
}

# These words make a stock query unnecessarily person-specific/staged. When an
# opening query asks for an identifiable human without a safe framing term, remove
# them locally and keep the concrete environment/objects. This does not judge the
# footage; Gemini Vision remains the final fail-closed safety/relevance gate.
_OPENING_QUERY_DROP_TERMS = _HUMAN_QUERY_TERMS | {
    "a",
    "an",
    "the",
    "by",
    "at",
    "in",
    "on",
    "with",
    "near",
    "and",
    "of",
    "from",
    "into",
    "during",
    "sitting",
    "seated",
    "standing",
    "walking",
    "looking",
    "watching",
    "thinking",
    "pensively",
    "reflecting",
    "resting",
    "focused",
    "focus",
    "calm",
    "alone",
    "wooden",
    "sunlit",
    "empty",
    "tall",
    "glass",
    "morning",
    "late",
    "afternoon",
    "warm",
    "natural",
    "simple",
    "clean",
    "minimalist",
    "realistic",
    "cinematic",
    "shot",
    "quiet",
}

_OPENING_QUERY_FALLBACK = "quiet room natural light"
_SPEAKER_PREFIX_RE = re.compile(r"^(?:A|B)\s*:\s*", re.IGNORECASE)
_FIRST_SENTENCE_RE = re.compile(r"^(.+?[؟?!\.])(?:\s|$)", re.DOTALL)

# Run54 proved that a full-script repair can still reproduce a harsh coercive
# imperative even after the repair prompt explicitly names preachiness. Keep this
# normalization deliberately tiny: these are the observed "wean yourself" forms,
# not a general Arabic imperative blacklist. Ordinary practical suggestions such as
# "جرب أن تدوّن" or "خذ نفسًا" remain untouched and the independent tone auditor
# still makes the final pass/block decision.
_HARSH_DIRECTIVE_PATTERNS = (
    (re.compile(r"افطم\s+نفسك\s+تدريجي(?:ًا|اً|ا)\s+عن\s+"), "يمكنك أن تقلل تدريجيًا من "),
    (re.compile(r"افطم\s+نفسك\s+عن\s+"), "يمكنك أن تقلل من "),
)


def _single_use_transition_slots(transition_variants: list[str], section_count: int) -> list[str]:
    """Return one transition slot per post-opening section without recycling hints.

    The Engine intentionally asks the outline for exactly three fresh transition
    variants. Reusing those three cyclically across seven film transitions creates
    deterministic phrase repetition (Run #41 repeated the same hint in sections
    2, 5 and 8). A hint may therefore be offered once only; remaining transitions
    are left empty so the writer connects ideas naturally in its own words.
    """
    slot_count = max(0, section_count - 1)
    variants = [str(value).strip() for value in transition_variants if str(value).strip()]
    used_once = variants[:slot_count]
    return used_once + [""] * max(0, slot_count - len(used_once))


def _safe_opening_visual_query(query: str) -> str:
    """Return a broad stock-search query when the opening asks for an identifiable human.

    This is intentionally deterministic and zero-call. It only intervenes when a
    query explicitly names a human subject and provides no non-identifiable framing.
    Safe framing and non-human queries pass through byte-for-byte.
    """
    original = str(query).strip()
    if not original:
        return original

    tokens = re.findall(r"[a-z]+", original.lower())
    if not tokens:
        return original
    if not any(token in _HUMAN_QUERY_TERMS for token in tokens):
        return original
    if any(token in _SAFE_FRAMING_TERMS for token in tokens):
        return original

    kept = [token for token in tokens if token not in _OPENING_QUERY_DROP_TERMS]
    # Preserve order while removing duplicates so Pexels/Pixabay receive a compact,
    # broad concept rather than another highly staged sentence.
    compact = list(dict.fromkeys(kept))
    if len(compact) < 2:
        return _OPENING_QUERY_FALLBACK
    return " ".join(compact[:6])


def _guard_opening_brief(outline: object, *, fmt: str) -> object:
    if fmt not in {"film", "story"} or not isinstance(outline, dict):
        return outline
    briefs = outline.get("section_briefs")
    if not isinstance(briefs, list) or not briefs or not isinstance(briefs[0], dict):
        return outline

    original = str(briefs[0].get("visual_query", "")).strip()
    safe = _safe_opening_visual_query(original)
    if safe and safe != original:
        briefs[0]["visual_query"] = safe[:260]
        print(f"Planner quality guard sanitized opening visual_query: {original} -> {safe}")
    return outline


def _neutralize_harsh_directives(text: str) -> str:
    """Neutralize only the coercive Run54 directive proven to trip tone QA.

    This is a deterministic text transform, not a replacement for the tone gate.
    It intentionally leaves all unrelated imperatives and advice wording unchanged.
    """
    original = str(text)
    normalized = original
    replaced = False
    for pattern, replacement in _HARSH_DIRECTIVE_PATTERNS:
        updated, count = pattern.subn(replacement, normalized)
        if count:
            replaced = True
            normalized = updated
    if replaced:
        # The exact blocked Run54 sentence also contained this agreement error. Keep
        # the grammar repair scoped to text in which the harsh directive was found.
        normalized = normalized.replace("من هذا المراقبة", "من هذه المراقبة")
    return normalized


def _normalize_plan_tone(plan: object) -> int:
    """Apply the bounded zero-call tone transform to section narration in place."""
    sections = getattr(plan, "sections", None)
    if not isinstance(sections, list):
        return 0
    changed = 0
    for section in sections:
        original = str(getattr(section, "narration", ""))
        normalized = _neutralize_harsh_directives(original)
        if normalized != original:
            section.narration = normalized
            changed += 1
            print(f"Planner quality guard neutralized harsh directive in section {getattr(section, 'id', '?')}")
    return changed


def _first_spoken_sentence(text: str) -> str:
    """Extract the first sentence the viewer actually hears, not planning metadata."""
    cleaned = str(text).strip()
    if not cleaned:
        return ""

    # Dialogue plans can begin with a speaker label. The identity of the speaker is
    # not part of the hook wording and should not affect anti-repetition similarity.
    cleaned = _SPEAKER_PREFIX_RE.sub("", cleaned, count=1).strip()
    match = _FIRST_SENTENCE_RE.match(cleaned)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()

    first_line = next((line.strip() for line in cleaned.splitlines() if line.strip()), cleaned)
    return re.sub(r"\s+", " ", first_line).strip()


def _spoken_hook(plan: object) -> str:
    sections = getattr(plan, "sections", None)
    if isinstance(sections, list) and sections:
        narration = str(getattr(sections[0], "narration", "")).strip()
        spoken = _first_spoken_sentence(narration)
        if spoken:
            return spoken
    return str(getattr(plan, "hook", "")).strip()


def _spoken_hook_from_history_record(record: dict) -> str:
    """Resolve the accepted viewer-facing hook from the already-written final plan.

    The Engine calls append_history only after render/quality/monetization acceptance,
    and plan.json exists by then. Reading it here keeps persistent novelty memory tied
    to what was actually narrated while preserving plan.hook for packaging metadata.
    """
    output = str(record.get("output", "")).strip()
    if not output:
        return str(record.get("hook", "")).strip()
    try:
        plan_path = (orchestrator.ROOT / output).parent / "plan.json"
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        sections = data.get("sections", []) if isinstance(data, dict) else []
        if isinstance(sections, list) and sections and isinstance(sections[0], dict):
            spoken = _first_spoken_sentence(str(sections[0].get("narration", "")))
            if spoken:
                return spoken
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return str(record.get("hook", "")).strip()


def install_planner_quality_guard() -> None:
    """Patch runtime quality semantics without adding provider calls or weakening gates."""
    staged._NARRATIVE_FORMATS["question_answer"] = _QUESTION_ANSWER_RUNTIME_RULE

    current_outline = staged._outline
    if not getattr(current_outline, "_isco_opening_visual_query_guard", False):
        @wraps(current_outline)
        def guarded_outline(*args, **kwargs):
            outline = current_outline(*args, **kwargs)
            fmt = str(kwargs.get("fmt", "")).strip().lower()
            return _guard_opening_brief(outline, fmt=fmt)

        guarded_outline._isco_opening_visual_query_guard = True
        staged._outline = guarded_outline

    current_write = staged._write_full_script
    if not getattr(current_write, "_isco_single_use_transition_guard", False):
        @wraps(current_write)
        def guarded_write_full_script(*args, **kwargs):
            briefs = kwargs.get("briefs")
            transitions = kwargs.get("transition_variants")
            if isinstance(briefs, list) and isinstance(transitions, list):
                kwargs["transition_variants"] = _single_use_transition_slots(transitions, len(briefs))
            return current_write(*args, **kwargs)

        guarded_write_full_script._isco_single_use_transition_guard = True
        staged._write_full_script = guarded_write_full_script

    # install_router() is called before this guard and its routed_build_plan reads
    # staged.build_plan dynamically. Wrapping staged.build_plan here therefore covers
    # both the initial plan and the single consolidated repair without adding calls.
    current_build = staged.build_plan
    if not getattr(current_build, "_isco_tone_directive_guard", False):
        @wraps(current_build)
        def guarded_build_plan(*args, **kwargs):
            plan = current_build(*args, **kwargs)
            _normalize_plan_tone(plan)
            return plan

        guarded_build_plan._isco_tone_directive_guard = True
        staged.build_plan = guarded_build_plan

    current_novelty = orchestrator._novelty_flags
    if not getattr(current_novelty, "_isco_spoken_hook_novelty_guard", False):
        @wraps(current_novelty)
        def guarded_novelty_flags(plan, music, *, auto_topic: bool):
            metadata_hook = str(getattr(plan, "hook", "")).strip()
            spoken_hook = _spoken_hook(plan)
            if not spoken_hook or spoken_hook == metadata_hook:
                return current_novelty(plan, music, auto_topic=auto_topic)

            # Reuse the Engine's unchanged novelty function and unchanged 0.78 hook
            # threshold. Only the input is corrected to the actual spoken opening.
            plan.hook = spoken_hook
            try:
                return current_novelty(plan, music, auto_topic=auto_topic)
            finally:
                plan.hook = metadata_hook

        guarded_novelty_flags._isco_spoken_hook_novelty_guard = True
        orchestrator._novelty_flags = guarded_novelty_flags

    current_append = orchestrator.append_history
    if not getattr(current_append, "_isco_spoken_hook_history_guard", False):
        @wraps(current_append)
        def guarded_append_history(record: dict):
            stored = dict(record)
            metadata_hook = str(stored.get("hook", "")).strip()
            spoken_hook = _spoken_hook_from_history_record(stored)
            if spoken_hook:
                stored["hook"] = spoken_hook
            if metadata_hook and spoken_hook and metadata_hook != spoken_hook:
                stored["metadata_hook"] = metadata_hook
            return current_append(stored)

        guarded_append_history._isco_spoken_hook_history_guard = True
        orchestrator.append_history = guarded_append_history

    print(
        "Planner quality guard installed: opening stock query deterministic safety; "
        "bounded harsh-directive tone normalization; spoken-hook novelty/history alignment; "
        "transition hints single-use; question_answer narration rule strengthened"
    )
