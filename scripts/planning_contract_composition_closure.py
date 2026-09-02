from __future__ import annotations

"""Shared Long + Short reset-evidence composition for Planning Stage Contract.

Run170 proved the existing Long and Short bounded reset owners were still keyed to the
historical outer RuntimeError sentence after Explicit Stage Contract moved provider
exhaustion behind PlanningStageError. This adapter normalizes trustworthy Groq reset
metadata once and feeds the already-certified Long/Short bounded recovery owners.

Checkpoint ownership intentionally does *not* live here. Run127 permits checkpoint
authority assignments only in the established checkpoint owners, and Run132's
checkpoint_namespace_guard owns the durable-document/logical-cache composition.
"""

import re
from dataclasses import dataclass

from scripts import planning_capacity_headroom as short_headroom
from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as long_recovery


_GROQ_RESET_SEGMENT_RE = re.compile(
    r"GROQ_TPM_WINDOW_BUSY_PRECHECK"
    r"(?:(?!\s\|\s).)*?\bmodel=([^\s|]+)"
    r"(?:(?!\s\|\s).)*?\breset_in=(\d+(?:\.\d+)?)s",
    flags=re.I | re.S,
)
_INSTALLED = False


@dataclass(frozen=True)
class GroqTerminalResetEvidence:
    model_name: str
    reset_seconds: float


def groq_terminal_reset_evidence(error: BaseException) -> GroqTerminalResetEvidence | None:
    """Return typed Groq reset evidence without depending on the outer error sentence."""
    if isinstance(error, stage_contract.PlanningStageError):
        if error.code not in {
            stage_contract.PlanningErrorCode.CAPACITY,
            stage_contract.PlanningErrorCode.PROVIDER_TRANSIENT,
        }:
            return None
        text = str(error.detail)
    else:
        text = str(error)

    match = _GROQ_RESET_SEGMENT_RE.search(text)
    if match is None:
        return None
    model_name = match.group(1).strip()
    if not model_name:
        return None
    try:
        reset_seconds = max(0.0, float(match.group(2)))
    except (TypeError, ValueError):
        return None
    return GroqTerminalResetEvidence(model_name=model_name, reset_seconds=reset_seconds)


def _short_terminal_reset_evidence(error: BaseException) -> tuple[str, float] | None:
    evidence = groq_terminal_reset_evidence(error)
    if evidence is None:
        return None
    if evidence.reset_seconds > short_headroom.SHORT_TERMINAL_RESET_WAIT_LIMIT_SECONDS:
        return None
    return evidence.model_name, evidence.reset_seconds


def _long_remaining_reset_seconds(error: BaseException) -> float | None:
    evidence = groq_terminal_reset_evidence(error)
    if evidence is None:
        return None
    if evidence.reset_seconds > long_recovery._TERMINAL_RESET_LIMIT_SECONDS:
        return None
    return evidence.reset_seconds


def _long_model_from_error(error: BaseException) -> str | None:
    evidence = groq_terminal_reset_evidence(error)
    return evidence.model_name if evidence is not None else None


def install_planning_contract_composition_closure() -> None:
    """Feed both existing bounded recovery owners from one typed evidence normalizer."""
    global _INSTALLED
    if _INSTALLED:
        return

    short_headroom._terminal_reset_evidence = _short_terminal_reset_evidence
    long_recovery._remaining_reset_seconds = _long_remaining_reset_seconds
    long_recovery._model_from_error = _long_model_from_error

    _INSTALLED = True
    print(
        "Planning reset composition closure installed: "
        "scope=Long+Short reset_evidence=shared_typed budgets=unchanged"
    )
