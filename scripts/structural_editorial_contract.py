"""Promote Engine structural editorial flags into the existing bounded Script Doctor.

Run #109 showed that ``editorial_room.structural_ai_flags`` could correctly identify
formulaic narration while remaining advisory. The historical repair branch solved that
with a second, Runner-owned provider loop. The current planner already owns a single
consolidated Script Doctor, so creating another provider/retry owner would regress the
architecture.

This contract therefore keeps one editorial owner across the existing repair lifecycle:
1. append authoritative structural flags to the deterministic issue list that already
   decides whether the existing Script Doctor runs;
2. measure the same Engine detector immediately after Script Doctor and before the
   existing append-only length repair, so a residual Doctor defect is distinguishable
   from a defect introduced by append-only text;
3. forbid append-only length repair from introducing new Arabic question-mark sentences;
4. re-run the same Engine detector on the final returned plan and fail closed if the
   bounded repair lifecycle did not clear the flags.

No provider call, retry loop, schema, threshold, or detector is implemented here.
"""

from __future__ import annotations

from functools import wraps

import isco_video_agent.resilient_planner as staged
from isco_video_agent.editorial_room import structural_ai_flags


_ISSUE_WRAPPER_MARKER = "_isco_structural_editorial_issue_owner"
_BUILD_WRAPPER_MARKER = "_isco_structural_editorial_final_gate"
_APPEND_WRAPPER_MARKER = "_isco_structural_editorial_pre_append_probe"

# Run #249 (real production log, Long/film): the bounded Script Doctor received only
# the flag's bare machine name ("excessive_rhetorical_questions") and still failed to
# clear it in its one allowed pass, failing the whole production closed. Run #265
# reproduced the family after a 739-word Doctor result entered append-only repair and
# finished at 1138 words, but the old logs could not prove whether the Doctor still
# carried the flag or append-only text reintroduced it. The pre-append probe below
# closes that observability gap without adding any inference or repair owner.
_RHETORICAL_QUESTIONS_FLAG = "excessive_rhetorical_questions"


def _joined_narration(sections: object) -> str:
    if not isinstance(sections, list):
        return ""
    return " ".join(
        " ".join(str(getattr(section, "narration", "") or "").strip().split())
        for section in sections
        if str(getattr(section, "narration", "") or "").strip()
    )


def _current_flags(sections: object) -> tuple[str, ...]:
    text = _joined_narration(sections)
    if not text:
        return ()
    return structural_ai_flags(text, short_form=False)


def _rhetorical_question_count(sections: object) -> int:
    """Diagnostic count using the exact Arabic mark observed by the Engine detector."""
    return _joined_narration(sections).count("؟")


def _structural_issue_note(flags: tuple[str, ...]) -> str:
    return (
        "The Engine Editorial Room authoritative structural detector reports: "
        + ", ".join(flags)
        + ". Clear every listed structural pattern while preserving the canonical "
        "editorial intent, evidence boundaries, section roles, and host-managed identity. "
        "Use the smallest natural rewrites needed; do not replace one formulaic pattern "
        "with another."
    )


def _rhetorical_question_guidance() -> str:
    return (
        f"DETERMINISTIC_ACCEPTANCE_RULE {_RHETORICAL_QUESTIONS_FLAG}: this machine "
        "label means too many sentences across the whole script end in the Arabic "
        "question mark (؟) rhetorically. Reread EVERY section before returning the "
        "corrected script. Convert every rhetorical question you can into a direct "
        "declarative sentence while preserving its meaning; do not merely remove the "
        "punctuation. Keep at most one genuinely necessary viewer-facing question in "
        "the whole script, and do not introduce any new question-mark sentence while "
        "repairing length, cadence, transitions, or section structure. This is a final "
        "acceptance requirement for this single bounded Doctor pass, not a suggestion."
    )


def _with_structural_issue_notes(original):
    @wraps(original)
    def wrapped(sections, *args, **kwargs):
        notes = list(original(sections, *args, **kwargs))
        flags = _current_flags(sections)
        if flags:
            notes.append(_structural_issue_note(flags))
            if _RHETORICAL_QUESTIONS_FLAG in flags:
                notes.append(_rhetorical_question_guidance())
            print(
                "Structural Editorial contract promoted advisory flags into existing "
                "Script Doctor: " + ", ".join(flags)
            )
        return notes

    setattr(wrapped, _ISSUE_WRAPPER_MARKER, True)
    return wrapped


def _with_pre_append_probe(original):
    """Measure Doctor output and keep append-only repair from owning rhetoric.

    ``_script_doctor_underlength_retry`` is the existing Engine hook entered only after
    the whole-script Doctor has returned and immediately before append-only length text
    is requested/applied. Wrapping this exact hook therefore gives the missing Run #265
    boundary measurement without another provider call or repair lifecycle.
    """

    @wraps(original)
    def wrapped(*args, **kwargs):
        sections = kwargs.get("sections")
        before_flags = _current_flags(sections)
        before_questions = _rhetorical_question_count(sections)
        residual = _RHETORICAL_QUESTIONS_FLAG in before_flags
        print(
            "Structural Editorial pre-append probe: "
            f"rhetorical_questions={before_questions} "
            f"excessive_rhetorical_questions={'true' if residual else 'false'} "
            "boundary=post_script_doctor_pre_append"
        )

        additions = original(*args, **kwargs)
        if not isinstance(additions, dict):
            return additions

        introduced_questions = sum(
            str(text or "").count("؟") for text in additions.values()
        )
        print(
            "Structural Editorial append-only probe: "
            f"introduced_question_marks={introduced_questions} "
            "policy=no_new_rhetorical_questions"
        )
        if introduced_questions:
            raise RuntimeError(
                "Structural Editorial append-only guard blocked new rhetorical questions: "
                f"pre_append_questions={before_questions} "
                f"introduced_question_marks={introduced_questions}"
            )
        return additions

    setattr(wrapped, _APPEND_WRAPPER_MARKER, True)
    return wrapped


def _with_structural_final_gate(original):
    @wraps(original)
    def wrapped(*args, **kwargs):
        plan = original(*args, **kwargs)
        flags = _current_flags(getattr(plan, "sections", None))
        if flags:
            raise RuntimeError(
                "Structural Editorial contract failed closed after bounded Script Doctor: "
                + ", ".join(flags)
            )
        return plan

    setattr(wrapped, _BUILD_WRAPPER_MARKER, True)
    return wrapped


def install_structural_editorial_contract() -> None:
    """Install deterministic structural closure without a second repair/provider owner."""
    current_issue_owner = staged._section_length_issue_notes
    if not getattr(current_issue_owner, _ISSUE_WRAPPER_MARKER, False):
        staged._section_length_issue_notes = _with_structural_issue_notes(current_issue_owner)

    current_append_owner = getattr(staged, "_script_doctor_underlength_retry", None)
    if current_append_owner is not None and not getattr(
        current_append_owner, _APPEND_WRAPPER_MARKER, False
    ):
        staged._script_doctor_underlength_retry = _with_pre_append_probe(current_append_owner)

    current_build = staged.build_plan
    if not getattr(current_build, _BUILD_WRAPPER_MARKER, False):
        staged.build_plan = _with_structural_final_gate(current_build)

    print(
        "Structural Editorial contract installed: Engine flags -> existing Script Doctor; "
        "post-Doctor pre-append probe=true append_new_questions=false "
        "post-repair fail-closed=true extra_provider_owner=false"
    )
