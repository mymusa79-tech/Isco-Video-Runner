from __future__ import annotations

import re
import time

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity


# Run #124 proved that fast failover must not become fast failure. Later runs proved the
# inverse risk: a bounded wait PER terminal shard can still amplify into minutes across
# a long Writer/Doctor graph. Run #208 exposed the remaining classification bug: an
# evidence-backed Groq TPM reset window was treated as payload pressure, so the normal
# 3-section semantic batch was recursively split 3 -> 2+1 -> 1+1 before the reset owner
# could act. That converted one temporal window into one sleep per section.
#
# Keep two failure families separate:
#   * payload/shape pressure -> existing bounded semantic sharding (3 -> 2+1 -> 1+1)
#   * temporal Groq TPM window -> wait once for the CURRENT semantic batch, then retry
#     that exact batch once. If that retry reveals real payload pressure, the existing
#     sharder remains authoritative and may split it normally.
#
# No quality gate, provider attempt budget, schema repair budget, or section limit is
# relaxed here. Recovery still requires provider reset evidence, remains <=60s per wait,
# is model-scoped, is retry-once per semantic batch/model, and has a finite run-wide cap.
_TERMINAL_RESET_LIMIT_SECONDS = 60.0
_TERMINAL_WAIT_LIMIT_SECONDS = 60.0
_RESET_SAFETY_SECONDS = 1.5
_MAX_LONGFORM_SECTIONS = max(batching.staged._SECTION_COUNTS.values())
_MAX_TERMINAL_WAIT_SECONDS_PER_RUN = _TERMINAL_WAIT_LIMIT_SECONDS * _MAX_LONGFORM_SECTIONS
_RESET_RE = re.compile(r"reset_in=(\d+(?:\.\d+)?)s", flags=re.I)
_MODEL_RE = re.compile(r"\bmodel=([^\s|]+)", flags=re.I)
_RECOVERED_TERMINAL_SHARDS: set[tuple[str, tuple[str, ...], str]] = set()
_TERMINAL_RECOVERY_COUNT = 0
_TERMINAL_WAIT_SPENT_SECONDS = 0.0


def _model_from_error(exc: BaseException) -> str | None:
    match = _MODEL_RE.search(str(exc))
    if match is None:
        return None
    model = match.group(1).strip()
    return model or None


def _is_groq_tpm_window_busy(exc: BaseException) -> bool:
    """Return True only for the typed temporal Groq TPM-window signal.

    This signal is deliberately NOT payload pressure. Splitting an unchanged temporal
    rate-limit window does not create capacity; it only multiplies waits and provider
    calls. The reset owner below handles it at the semantic batch boundary.
    """
    return "groq_tpm_window_busy_precheck" in str(exc).strip().lower()


def _remaining_reset_seconds(exc: BaseException) -> float | None:
    """Read reset evidence attached to the exact exhausted provider set.

    Run126 made Groq capacity state model-scoped. A process-global "last response"
    mirror is therefore not authority for terminal recovery because another model or
    provider call may have updated it before this outer wrapper handles the error.
    """
    text = str(exc)
    lower = text.lower()
    if "all free providers failed for planning subtask" not in lower:
        return None
    if "groq_tpm_window_busy_precheck" not in lower:
        return None

    match = _RESET_RE.search(text)
    if match is None:
        return None
    remaining = max(0.0, float(match.group(1)))
    if remaining > _TERMINAL_RESET_LIMIT_SECONDS:
        return None
    return remaining


def _clear_waited_model_window(exc: BaseException) -> None:
    model_name = _model_from_error(exc)
    if not model_name:
        return
    state = capacity._model_state(model_name)
    state["remaining_tokens"] = None
    state["reset_at_epoch"] = None
    capacity._persist_model_states()


def _run_wait_budget_allows(wait_seconds: float) -> bool:
    return _TERMINAL_WAIT_SPENT_SECONDS + wait_seconds <= _MAX_TERMINAL_WAIT_SECONDS_PER_RUN


def install_run124_terminal_provider_recovery() -> None:
    if getattr(batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY", False):
        return

    original_call = batching._call_capacity_aware_shard
    original_is_transport_pressure = batching._is_transport_pressure

    # Run208 family closure: temporal TPM windows must bubble to this batch-level reset
    # owner before planning_batch_hardening can recursively split them. Keep every real
    # payload/context/output pressure marker delegated to the existing splitter.
    def split_only_transport_pressure(exc: BaseException) -> bool:
        if _is_groq_tpm_window_busy(exc):
            return False
        return original_is_transport_pressure(exc)

    batching._is_transport_pressure = split_only_transport_pressure

    def bounded_terminal_call(
        api_key: str,
        model: str,
        ids: list[str],
        *,
        prompt_builder,
        label: str,
    ) -> dict[str, dict]:
        global _TERMINAL_RECOVERY_COUNT, _TERMINAL_WAIT_SPENT_SECONDS
        try:
            return original_call(
                api_key,
                model,
                ids,
                prompt_builder=prompt_builder,
                label=label,
            )
        except RuntimeError as exc:
            remaining = _remaining_reset_seconds(exc)
            waited_model = _model_from_error(exc) or model
            key = (label, tuple(ids), waited_model)
            if remaining is None or key in _RECOVERED_TERMINAL_SHARDS:
                raise

            wait_seconds = min(
                remaining + _RESET_SAFETY_SECONDS,
                _TERMINAL_WAIT_LIMIT_SECONDS,
            )
            if not _run_wait_budget_allows(wait_seconds):
                print(
                    "Run124 terminal provider recovery skipped by topology-derived run-wide retry budget: "
                    f"label={label} sections={','.join(ids)} requested_wait={wait_seconds:.2f}s "
                    f"recoveries={_TERMINAL_RECOVERY_COUNT} "
                    f"wait_spent={_TERMINAL_WAIT_SPENT_SECONDS:.2f}/{_MAX_TERMINAL_WAIT_SECONDS_PER_RUN:.0f}s"
                )
                raise

            _RECOVERED_TERMINAL_SHARDS.add(key)
            _TERMINAL_RECOVERY_COUNT += 1
            _TERMINAL_WAIT_SPENT_SECONDS += wait_seconds
            print(
                "Run124 coordinated TPM-window recovery: "
                f"label={label} sections={','.join(ids)} groq_model={waited_model} "
                f"groq_reset_in={remaining:.2f}s wait={wait_seconds:.2f}s "
                "action=retry_same_semantic_batch_once "
                f"run_recovery={_TERMINAL_RECOVERY_COUNT} "
                f"run_wait_spent={_TERMINAL_WAIT_SPENT_SECONDS:.2f}s"
            )
            time.sleep(wait_seconds)
            _clear_waited_model_window(exc)

            # Exactly one retry of the same semantic batch. If this call encounters
            # genuine payload pressure, original_call's existing sharder may split it;
            # if the same TPM window recurs it bubbles out because this wrapper does not
            # recursively call itself and therefore cannot create a wait loop.
            return original_call(
                api_key,
                model,
                ids,
                prompt_builder=prompt_builder,
                label=label,
            )

    batching._call_capacity_aware_shard = bounded_terminal_call
    batching._ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY = True
    print(
        "Run124 coordinated terminal provider recovery installed: "
        "groq_window_is_temporal_not_payload=true model_scoped_reset=true "
        "semantic_batch_wait<=60s retry_same_batch_once=true payload_split_owner=existing "
        "recovery_count=telemetry_only "
        f"run_wait_cap={_MAX_TERMINAL_WAIT_SECONDS_PER_RUN:.0f}s "
        f"topology_sections={_MAX_LONGFORM_SECTIONS}"
    )
