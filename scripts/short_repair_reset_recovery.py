from __future__ import annotations

from typing import Callable, TypeVar

from scripts import planning_capacity_headroom as headroom
from scripts import short_planning_repair


# Runs #158 and #160 proved that native Short Draft/Review terminal-reset recovery did
# not cover the compact RepairDossier transport. Both runs reached a valid Moment,
# entered the existing one-call surgical repair, then failed closed with trustworthy
# Groq reset evidence (~49s) while OpenRouter was preflight-blocked. The repair call is
# still a planning provider call, so reuse the same single bounded reset owner rather
# than inventing a second retry policy.
#
# This layer does not change Dossier max_attempts, provider limits, prompt envelopes,
# or any semantic/quality gate. It permits exactly the same evidence-backed wait + one
# retry already certified for native Short Draft/Review.
_T = TypeVar("_T")
_INSTALLED = False


def _with_short_repair_terminal_recovery(call: Callable[[], _T]) -> _T:
    return headroom._short_provider_call_with_terminal_recovery(  # type: ignore[return-value]
        call,  # type: ignore[arg-type]
        phase="repair",
    )


def install_short_repair_reset_recovery() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original = short_planning_repair._repair_existing_moment

    def repair_with_reset_recovery(*args, **kwargs):
        return _with_short_repair_terminal_recovery(
            lambda: original(*args, **kwargs)
        )

    short_planning_repair._repair_existing_moment = repair_with_reset_recovery
    _INSTALLED = True
    print(
        "Short Dossier terminal reset recovery installed: "
        "provider_evidence_only=true wait<=60s retry_once=true "
        "dossier_attempt_budget=unchanged"
    )
