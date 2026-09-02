from __future__ import annotations

"""Shared Long + Short composition closure for Planning Stage Contract runtime.

Run170 exposed two regressions created at layer boundaries rather than inside the
individual owners themselves:

1. Stage Contract changed the terminal provider failure envelope from the historical
   ``All free providers failed for planning subtask`` text to ``PlanningStageError``
   aggregation. The certified Long and Short bounded reset owners still looked for the
   old sentence, so trustworthy Groq ``reset_in<=60s`` evidence became invisible.
2. Stage Contract's logical cache schema uses version 2, while authenticated durable
   planning persistence deliberately owns an on-disk document schema version 1. Run132
   had already established that namespace/cache evolution must not mutate the durable
   document version, but the newer Stage Contract wrote version 2 directly to the same
   file and recreated the collision.

This module is a composition adapter only. It does not change provider order, provider
limits, retry counts, prompt/schema semantics, quality gates, or durable cryptography.
It normalizes provider reset evidence once for both Long and Short recovery owners and
keeps Stage cache versioning separate from the durable checkpoint document version.
"""

import json
import re
from dataclasses import dataclass

from scripts import planning_capacity_headroom as short_headroom
# Run130 contract: compatibility/storage core is reachable only through this wrapper,
# which binds the single runtime-phase authority before exporting core functions.
from scripts import planning_checkpoint_state as durable_state
from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as long_recovery


DURABLE_CHECKPOINT_DOCUMENT_VERSION = 1
STAGE_CACHE_LOGICAL_VERSION = 2
STAGE_CACHE_VERSION_FIELD = "stage_contract_cache_version"

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
    """Normalize trustworthy Groq short-window reset evidence from one failure boundary.

    Stage Contract errors are admitted only from provider/capacity failure classes.
    Historical RuntimeError envelopes remain supported for already-certified recovery
    tests and non-Stage compatibility paths. Consumers receive typed evidence instead
    of depending on either top-level error sentence.
    """
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


def _fresh_stage_cache() -> dict:
    return {"version": STAGE_CACHE_LOGICAL_VERSION, "responses": {}}


def _load_stage_checkpoint() -> dict:
    """Read durable-v1 bytes and expose Stage-v2 authority only when explicitly marked."""
    path = stage_contract.router.CACHE_PATH
    if not path.exists():
        return _fresh_stage_cache()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            f"checkpoint_json_unreadable:{type(exc).__name__}",
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("responses", {}), dict):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            "checkpoint_root_shape_invalid",
        )

    document_version = data.get("version")
    logical_version = data.get(STAGE_CACHE_VERSION_FIELD)

    # Run170 pre-fix processes could create an ephemeral v2 document before failing to
    # persist it durably. Accept it as the same logical Stage-v2 cache and migrate it to
    # durable-v1 on the next write. It can never arrive from authenticated durable
    # restore because that layer correctly rejected v2 bytes.
    if document_version == STAGE_CACHE_LOGICAL_VERSION:
        return {
            "version": STAGE_CACHE_LOGICAL_VERSION,
            "responses": dict(data.get("responses", {})),
        }

    # A v1 durable document without the Stage-v2 marker may belong to historical
    # prompt-hash authority. Never promote it into explicit Stage Contract authority.
    if (
        document_version != DURABLE_CHECKPOINT_DOCUMENT_VERSION
        or logical_version != STAGE_CACHE_LOGICAL_VERSION
    ):
        return _fresh_stage_cache()

    return {
        "version": STAGE_CACHE_LOGICAL_VERSION,
        "responses": dict(data.get("responses", {})),
    }


def _save_stage_checkpoint(checkpoint: dict) -> None:
    """Persist Stage-v2 cache authority inside the durable-v1 document envelope."""
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("version") != STAGE_CACHE_LOGICAL_VERSION
        or not isinstance(checkpoint.get("responses"), dict)
    ):
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            "stage_checkpoint_shape_invalid",
        )

    payload = {
        "version": DURABLE_CHECKPOINT_DOCUMENT_VERSION,
        STAGE_CACHE_VERSION_FIELD: STAGE_CACHE_LOGICAL_VERSION,
        "responses": dict(checkpoint["responses"]),
    }
    try:
        # Compose against the real authenticated durable normalizer through the Run130
        # authority wrapper before bytes are written. A future drift therefore fails at
        # the writer, not only during run-end persistence.
        durable_state._normalize_checkpoint(payload)
    except (OSError, ValueError) as exc:
        raise stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.CHECKPOINT_INVALID,
            f"durable_checkpoint_contract_rejected:{type(exc).__name__}:{str(exc)[:240]}",
        ) from exc

    path = stage_contract.router.CACHE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def install_planning_contract_composition_closure() -> None:
    """Install one shared compatibility owner after historical Long/Short owners exist."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Both bounded recovery owners now consume the same typed evidence normalizer. The
    # actual wait/retry budgets stay owned by their existing Long and Short modules.
    short_headroom._terminal_reset_evidence = _short_terminal_reset_evidence
    long_recovery._remaining_reset_seconds = _long_remaining_reset_seconds
    long_recovery._model_from_error = _long_model_from_error

    # Stage Contract keeps logical cache v2 semantics, while exact persisted bytes stay
    # compatible with authenticated durable checkpoint document v1 from Run132.
    stage_contract._load_checkpoint_strict = _load_stage_checkpoint
    stage_contract._save_checkpoint = _save_stage_checkpoint

    _INSTALLED = True
    print(
        "Planning contract composition closure installed: "
        "scope=Long+Short reset_evidence=shared_typed "
        "stage_cache=v2 durable_document=v1"
    )
