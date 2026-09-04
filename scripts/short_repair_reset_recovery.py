from __future__ import annotations

from typing import Callable, TypeVar

from scripts import planning_capacity_headroom as headroom
from scripts.planning_contract_composition_closure import (
    install_planning_contract_composition_closure,
)
from scripts.planning_repair_identity_family import install_planning_repair_identity_family
from scripts import short_planning_repair
from scripts.short_stage_retry_composition import install_short_stage_retry_composition


# Runs #158 and #160 proved that native Short Draft/Review terminal-reset recovery did
# not cover the compact RepairDossier transport. Run170 then proved that the newer
# Explicit Stage Contract changed the outer provider-failure envelope and accidentally
# made the existing reset owner blind to trustworthy Groq evidence. Run172 proved the
# next composition edge: after that bounded retry succeeded, the native-Short Stage
# Contract counted the retry transport as a second logical lifecycle stage. Run197 then
# proved the final ownership edge: a retry could return usable mutable repair content
# while paraphrasing plan-level topic identity before the Engine had a chance to rebind
# the canonical approved topic.
#
# Activate the shared repair-identity family plus both retry composition owners at this
# stable runtime seam, then reuse the exact same bounded Short recovery owner as before.
# This layer does not change Dossier max_attempts, provider limits, prompt envelopes,
# retry budgets, or any semantic/quality gate.
_T = TypeVar("_T")
_INSTALLED = False


def _with_short_repair_terminal_recovery(call: Callable[[], _T]) -> _T:
    return headroom._short_provider_call_with_terminal_recovery(  # type: ignore[return-value]
        call,  # type: ignore[arg-type]
        phase="repair",
    )


def install_short_repair_reset_recovery() -> None:
    global _INSTALLED

    # Canonical runtime reaches this seam only after the explicit Stage Contract and
    # Short headroom/reset owner exist, so it is the single place where their retry and
    # immutable repair-identity semantics can be composed without inventing another
    # installer order. Long is certified by the family but remains behaviorally
    # unchanged because its existing dossier transport is already a section-only patch.
    install_planning_repair_identity_family()
    install_planning_contract_composition_closure()
    install_short_stage_retry_composition()

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
