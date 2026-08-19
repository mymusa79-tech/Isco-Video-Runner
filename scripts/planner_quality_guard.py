from __future__ import annotations

import re
from functools import wraps

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


def install_planner_quality_guard() -> None:
    """Patch planner output/prompting only; provider call counts and downstream gates stay unchanged."""
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

    print(
        "Planner quality guard installed: opening stock query deterministic safety; "
        "transition hints single-use; question_answer narration rule strengthened"
    )
