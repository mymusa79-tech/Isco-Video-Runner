from __future__ import annotations

from functools import wraps

import isco_video_agent.resilient_planner as staged


_QUESTION_ANSWER_RUNTIME_RULE = (
    "Question-and-answer structure: raise sincere viewer questions, answer them with layered analysis, never a repetitive FAQ list. "
    "The SPOKEN narration itself must audibly move through natural questions and answers across the episode; do not collapse "
    "question_answer into continuous expository monologue with the format present only in metadata. Questions must deepen the "
    "argument and each answer must advance it."
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


def install_planner_quality_guard() -> None:
    """Patch planner prompting only; provider call counts and quality gates stay unchanged."""
    staged._NARRATIVE_FORMATS["question_answer"] = _QUESTION_ANSWER_RUNTIME_RULE

    current = staged._write_full_script
    if getattr(current, "_isco_single_use_transition_guard", False):
        print("Planner quality guard already installed")
        return

    @wraps(current)
    def guarded_write_full_script(*args, **kwargs):
        briefs = kwargs.get("briefs")
        transitions = kwargs.get("transition_variants")
        if isinstance(briefs, list) and isinstance(transitions, list):
            kwargs["transition_variants"] = _single_use_transition_slots(transitions, len(briefs))
        return current(*args, **kwargs)

    guarded_write_full_script._isco_single_use_transition_guard = True
    staged._write_full_script = guarded_write_full_script
    print("Planner quality guard installed: transition hints single-use; question_answer narration rule strengthened")
