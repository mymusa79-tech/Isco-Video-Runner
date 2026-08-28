from __future__ import annotations

import re
import time

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity


# Run #124 proved that fast failover must not become fast failure.  Run #123 correctly
# stopped serializing every planning shard behind Groq's TPM window, but when a shard
# has already been reduced to one section and every alternate provider has failed, a
# known-near Groq reset is the cheapest remaining recovery path.  Waiting <=60 seconds
# once is materially safer than failing an otherwise healthy production run.
#
# This is transport recovery only.  It does not change prompts, section word gates,
# schemas, provider attempt ceilings, quality gates, factuality rules or model order.
_TERMINAL_RESET_LIMIT_SECONDS = 60.0
_RESET_SAFETY_SECONDS = 1.5
_RESET_RE = re.compile(r"reset_in=(\d+(?:\.\d+)?)s", flags=re.I)
_RECOVERED_TERMINAL_SHARDS: set[tuple[str, tuple[str, ...], str]] = set()


def _remaining_reset_seconds(exc: BaseException) -> float | None:
    text = str(exc)
    lower = text.lower()
    if "all free providers failed for planning subtask" not in lower:
        return None
    if "groq_tpm_window_busy_precheck" not in lower:
        return None

    now = time.monotonic()
    reset_at = capacity._GROQ_RATE_STATE.get("reset_at_monotonic")
    if isinstance(reset_at, (int, float)):
        remaining = max(0.0, float(reset_at) - now)
    else:
        match = _RESET_RE.search(text)
        if match is None:
            return None
        remaining = max(0.0, float(match.group(1)))

    # Do not turn this narrow recovery into another long planner sleep.  If the token
    # window is farther away than one minute, preserve the existing fail-fast result.
    if remaining > _TERMINAL_RESET_LIMIT_SECONDS:
        return None
    return remaining


def install_run124_terminal_provider_recovery() -> None:
    if getattr(batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY", False):
        return

    original_call = batching._call_capacity_aware_shard

    # Groq window pressure is itself a legitimate transport-pressure signal.  This
    # lets the existing bounded 3 -> 2+1 -> 1 splitter reduce a shard even when the
    # alternate provider fails for a different transient reason.
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

            _RECOVERED_TERMINAL_SHARDS.add(key)
            wait_seconds = remaining + _RESET_SAFETY_SECONDS
            print(
                "Run124 terminal provider recovery: "
                f"label={label} section={ids[0]} groq_reset_in={remaining:.2f}s "
                f"wait={wait_seconds:.2f}s action=single_bounded_retry"
            )
            time.sleep(wait_seconds)

            # The observed reset window has elapsed.  Clear only the local advisory
            # state; the next real Groq response will repopulate authoritative headers.
            capacity._GROQ_RATE_STATE["remaining_tokens"] = None
            capacity._GROQ_RATE_STATE["reset_at_monotonic"] = None

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
        "groq_window_is_transport_pressure terminal_single_shard_wait<=60s retry_once=true"
    )
