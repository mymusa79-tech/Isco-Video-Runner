from __future__ import annotations

from functools import wraps

import isco_video_agent.resilient_planner as staged
import scripts.task_level_planner_router as router


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


def _provider_states(task_router) -> list[tuple[str, str]]:
    """Read router closure state for diagnostics only; never mutate routing state."""
    try:
        cells = task_router.__closure__ or ()
        values = {
            name: cell.cell_contents
            for name, cell in zip(task_router.__code__.co_freevars, cells)
        }
        cooldown = set(values.get("cooldown", set()))
        providers = values.get("providers", [])
        states: list[tuple[str, str]] = []
        for item in providers:
            if not isinstance(item, tuple) or not item:
                continue
            name = str(item[0])
            states.append((name, "skipped=cooldown" if name in cooldown else "eligible"))
        return states
    except Exception:
        return []


def install_planner_quality_guard() -> None:
    """Patch planner prompting only; provider call counts and quality gates stay unchanged."""
    staged._NARRATIVE_FORMATS["question_answer"] = _QUESTION_ANSWER_RUNTIME_RULE

    current = staged._write_full_script
    if not getattr(current, "_isco_single_use_transition_guard", False):
        @wraps(current)
        def guarded_write_full_script(*args, **kwargs):
            print("PLANNING_BOUNDARY ENTER planner_quality_guard")
            try:
                briefs = kwargs.get("briefs")
                transitions = kwargs.get("transition_variants")
                if isinstance(briefs, list) and isinstance(transitions, list):
                    kwargs["transition_variants"] = _single_use_transition_slots(transitions, len(briefs))
                result = current(*args, **kwargs)
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:220]
                print(
                    "PLANNING_BOUNDARY ERROR planner_quality_guard "
                    + f"type={type(exc).__name__} detail={detail}"
                )
                raise
            print("PLANNING_BOUNDARY EXIT planner_quality_guard")
            return result

        guarded_write_full_script._isco_single_use_transition_guard = True
        staged._write_full_script = guarded_write_full_script

    current_router = staged.json_text
    if not getattr(current_router, "_isco_resilient_router_trace", False):
        @wraps(current_router)
        def traced_router(*args, **kwargs):
            print("PLANNING_BOUNDARY ENTER resilient_router")
            for provider_name, state in _provider_states(current_router):
                print(f"PROVIDER_STATE provider={provider_name} state={state}")
            try:
                result = current_router(*args, **kwargs)
            except Exception as exc:
                detail = str(exc).replace("\n", " ")[:220]
                print(
                    "PLANNING_BOUNDARY ERROR resilient_router "
                    + f"type={type(exc).__name__} detail={detail}"
                )
                raise
            print("PLANNING_BOUNDARY EXIT resilient_router")
            return result

        traced_router._isco_resilient_router_trace = True
        staged.json_text = traced_router

    current_gemini = router.gemini_json_text
    if not getattr(current_gemini, "_isco_provider_attempt_trace", False):
        @wraps(current_gemini)
        def traced_gemini(*args, **kwargs):
            model = str(kwargs.get("model", ""))
            print(f"PROVIDER_ATTEMPT provider=gemini model={model}")
            return current_gemini(*args, **kwargs)

        traced_gemini._isco_provider_attempt_trace = True
        router.gemini_json_text = traced_gemini

    current_openrouter = router.openrouter_json_text
    if not getattr(current_openrouter, "_isco_provider_attempt_trace", False):
        @wraps(current_openrouter)
        def traced_openrouter(*args, **kwargs):
            model = str(kwargs.get("model", ""))
            provider_name = (
                "openrouter-free-router"
                if model == "openrouter/free"
                else "openrouter-gpt-oss-free"
            )
            print(f"PROVIDER_ATTEMPT provider={provider_name} model={model}")
            return current_openrouter(*args, **kwargs)

        traced_openrouter._isco_provider_attempt_trace = True
        router.openrouter_json_text = traced_openrouter

    print("Planner quality guard installed: transition hints single-use; question_answer narration rule strengthened")
