from __future__ import annotations

import re
import time

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity


# Run #124 proved that fast failover must not become fast failure. Run #125 then proved
# the inverse risk: a bounded wait PER shard can still accumulate into minutes across a
# long Writer/Doctor graph. Keep the edge-case recovery bounded by both a per-recovery
# cap and a run-wide cap. Run132 proved the previous 60-second run cap contradicted the
# advertised three-recovery allowance: one legitimate ~49s reset made a second ~41s
# reset impossible. The public contract is now literal: every actual recovery sleep is
# <=60s, at most three recoveries are allowed, and the run-wide wait ceiling is 180s.
_TERMINAL_WAIT_LIMIT_SECONDS = 60.0
_RESET_SAFETY_SECONDS = 1.5
# Reserve the safety tail inside the 60-second actual-sleep contract rather than adding
# it on top. This keeps the printed/runtime contract and the aggregate 3*60s budget true.
_TERMINAL_RESET_LIMIT_SECONDS = _TERMINAL_WAIT_LIMIT_SECONDS - _RESET_SAFETY_SECONDS
_MAX_TERMINAL_RECOVERIES_PER_RUN = 3
_MAX_TERMINAL_WAIT_SECONDS_PER_RUN = (
    _TERMINAL_WAIT_LIMIT_SECONDS * _MAX_TERMINAL_RECOVERIES_PER_RUN
)
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


def _remaining_reset_seconds(exc: BaseException) -> float | None:
    """Read the reset evidence attached to the exact model failure.

    Run126 made Groq capacity state model-scoped. A process-global "last response"
    mirror is therefore no longer an authority for terminal recovery because another
    model/provider call may have updated it before this outer wrapper handles the error.
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
    if _TERMINAL_RECOVERY_COUNT >= _MAX_TERMINAL_RECOVERIES_PER_RUN:
        return False
    return _TERMINAL_WAIT_SPENT_SECONDS + wait_seconds <= _MAX_TERMINAL_WAIT_SECONDS_PER_RUN


def install_run124_terminal_provider_recovery() -> None:
    if getattr(batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY", False):
        return

    original_call = batching._call_capacity_aware_shard

    if "groq_tpm_window_busy_precheck" not in batching._TRANSPORT_PRESSURE_MARKERS:
        batching._TRANSPORT_PRESSURE_MARKERS = tuple(batching._TRANSPORT_PRESSURE_MARKERS) + (
            "groq_tpm_window_busy_precheck",
        )

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
            if len(ids) != 1:
                raise

            remaining = _remaining_reset_seconds(exc)
            key = (label, tuple(ids), model)
            if remaining is None or key in _RECOVERED_TERMINAL_SHARDS:
                raise

            wait_seconds = remaining + _RESET_SAFETY_SECONDS
            if not _run_wait_budget_allows(wait_seconds):
                print(
                    "Run124 terminal provider recovery skipped by run-wide retry budget: "
                    f"label={label} section={ids[0]} requested_wait={wait_seconds:.2f}s "
                    f"recoveries={_TERMINAL_RECOVERY_COUNT}/{_MAX_TERMINAL_RECOVERIES_PER_RUN} "
                    f"wait_spent={_TERMINAL_WAIT_SPENT_SECONDS:.2f}/{_MAX_TERMINAL_WAIT_SECONDS_PER_RUN:.0f}s"
                )
                raise

            _RECOVERED_TERMINAL_SHARDS.add(key)
            _TERMINAL_RECOVERY_COUNT += 1
            _TERMINAL_WAIT_SPENT_SECONDS += wait_seconds
            waited_model = _model_from_error(exc) or "unknown"
            print(
                "Run124 terminal provider recovery: "
                f"label={label} section={ids[0]} groq_model={waited_model} "
                f"groq_reset_in={remaining:.2f}s wait={wait_seconds:.2f}s "
                "action=single_bounded_retry "
                f"run_recovery={_TERMINAL_RECOVERY_COUNT}/{_MAX_TERMINAL_RECOVERIES_PER_RUN} "
                f"run_wait_spent={_TERMINAL_WAIT_SPENT_SECONDS:.2f}s"
            )
            time.sleep(wait_seconds)
            _clear_waited_model_window(exc)

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
        "Run124 terminal provider recovery installed: "
        "groq_window_is_transport_pressure model_scoped_reset=true "
        "terminal_single_shard_wait<=60s retry_once_per_shard=true "
        f"run_recovery_cap={_MAX_TERMINAL_RECOVERIES_PER_RUN} "
        f"run_wait_cap={_MAX_TERMINAL_WAIT_SECONDS_PER_RUN:.0f}s"
    )
