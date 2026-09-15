"""Promote Engine structural editorial flags into the existing bounded Script Doctor.

Run #109 showed that ``editorial_room.structural_ai_flags`` could correctly identify
formulaic narration while remaining advisory. The historical repair branch solved that
with a second, Runner-owned provider loop. The current planner already owns a single
consolidated Script Doctor, so creating another provider/retry owner would regress the
architecture.

This contract therefore does only two things:
1. append authoritative structural flags to the deterministic issue list that already
   decides whether the existing Script Doctor runs;
2. re-run the same Engine detector on the final returned plan and fail closed if the
   bounded Doctor did not clear the flags.

No provider call, retry loop, schema, threshold, or detector is implemented here.
"""

from __future__ import annotations

from functools import wraps

import isco_video_agent.resilient_planner as staged
from isco_video_agent.editorial_room import structural_ai_flags


_ISSUE_WRAPPER_MARKER = "_isco_structural_editorial_issue_owner"
_BUILD_WRAPPER_MARKER = "_isco_structural_editorial_final_gate"

# Run #249 (real production log, Long/film): the bounded Script Doctor received only
# the flag's bare machine name ("excessive_rhetorical_questions") and still failed to
# clear it in its one allowed pass, failing the whole production closed. Run #265 then
# reproduced the same family after a three-batch Doctor and a later append-only length
# repair. The pre-append owner now measures which side actually retained/introduced the
# flag; this guidance only strengthens the already-existing Doctor pass and adds no call.
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
        "label means too many sentences across the whole script end in a question mark "
        "(؟) used rhetorically rather than to genuinely ask the viewer something. Reread "
        "every section's narration and rephrase every nonessential question-mark sentence "
        "into a direct statement while preserving its meaning and section role. Do not "
        "introduce new ؟ sentences in rewritten sections. Before returning the complete "
        "repaired script, perform one final pass across every section and keep rewriting "
        "until this exact detector flag is clear; a merely smaller but still flagged set "
        "is not an acceptable repair."
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
    """Install deterministic Run #109 closure without adding a provider/retry owner."""
    current_issue_owner = staged._section_length_issue_notes
    if not getattr(current_issue_owner, _ISSUE_WRAPPER_MARKER, False):
        staged._section_length_issue_notes = _with_structural_issue_notes(current_issue_owner)

    current_build = staged.build_plan
    if not getattr(current_build, _BUILD_WRAPPER_MARKER, False):
        staged.build_plan = _with_structural_final_gate(current_build)

    print(
        "Structural Editorial contract installed: Engine flags -> existing Script Doctor; "
        "post-Doctor fail-closed=true extra_provider_owner=false"
    )
