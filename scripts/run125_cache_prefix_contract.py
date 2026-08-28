from __future__ import annotations

from provider_failure import ProviderFailure

from scripts import provider_capacity_hardening as capacity
from scripts import run125_capacity_routing_closure as closure


_PRODUCTION_GROQ_MODEL_POOL = (
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
)
_TPM_ERROR_MARKERS = (
    "tokens per minute",
    "(tpm)",
    " tpm:",
    "groq_tpm_window_busy_precheck",
)


def _writer_cache_layout(prompt: str) -> str:
    """Move every writer shard-specific field behind the common prompt prefix.

    Groq prompt caching is exact-prefix based. The existing writer prompt starts with a
    stable role sentence but puts `Write ONLY global sections X-Y` immediately after it,
    so every shard diverges before the expensive shared policy/research/persona body.
    Preserve the original text verbatim apart from whitespace at block joins and move
    only shard-specific context to the tail.
    """
    text = prompt
    dynamic: list[str] = []

    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("Write ONLY global sections "):
            dynamic.append(lines.pop(index))
            text = "".join(lines)
            break

    text, block = closure._extract_block(
        text,
        "PREVIOUS_WRITTEN_KEY_POINTS (context only; do not repeat their role):",
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
    )
    if block:
        dynamic.append(block)
    text, block = closure._extract_block(
        text,
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
        "Hard writing rules for every returned section:",
    )
    if block:
        dynamic.append(block)
    text, block = closure._extract_block(text, "GLOBAL POSITION RULES:", "EDITORIAL_POLICY:")
    if block:
        dynamic.append(block)

    batch_start = text.find(
        "BATCH_SECTION_SPECS — write exactly one narration per entry in this exact order:"
    )
    if batch_start >= 0:
        dynamic.append(text[batch_start:])
        text = text[:batch_start]

    if not dynamic:
        return prompt
    return (
        text.rstrip()
        + f"\n\n{closure._CACHE_LAYOUT_MARKER}\n"
        + "DYNAMIC_BATCH_CONTEXT — transport-specific values follow the shared cached prefix:\n"
        + "\n".join(part.strip() for part in dynamic if part.strip())
        + "\n"
    )


def _is_tpm_window_exhausted(error) -> bool:
    """True only for short-window Groq token throttling, never daily quota exhaustion."""
    if closure._is_tpd_exhausted(error):
        return False
    lower = str(error).lower()
    if "groq_tpm_window_busy_precheck" in lower:
        return True
    is_rate_limited = (
        "429" in lower
        or "rate_limit_exceeded" in lower
        or "rate limit" in lower
        or "rate limited" in lower
    )
    return is_rate_limited and any(marker in lower for marker in _TPM_ERROR_MARKERS)


def _retry_after_seconds() -> float | None:
    value = closure.router._last_call_rate_limit_headers.get("retry_after")
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _model_reset_seconds(model_name: str) -> float | None:
    """Use the longest trustworthy provider reset signal; never invent or truncate it."""
    waits: list[float] = []
    state = capacity._model_state(model_name)
    reset_at_epoch = state.get("reset_at_epoch")
    if isinstance(reset_at_epoch, (int, float)):
        waits.append(max(0.0, float(reset_at_epoch) - capacity.time.time()))
    retry_after = _retry_after_seconds()
    if retry_after is not None:
        waits.append(retry_after)
    return max(waits) if waits else None


def _terminal_tpm_window_error(error) -> RuntimeError:
    model_name = closure._active_groq_model()
    state = capacity._model_state(model_name)
    remaining = state.get("remaining_tokens")
    reset_seconds = _model_reset_seconds(model_name)
    reset_fragment = "unknown" if reset_seconds is None else f"{reset_seconds:.2f}s"
    return RuntimeError(
        "GROQ_TPM_WINDOW_BUSY_PRECHECK "
        f"model={model_name} remaining={remaining} reset_in={reset_fragment} "
        "action=provider_evidence_failover_without_partial_retry"
    )


def _retry_after_exceeds_local_budget() -> bool:
    retry_after = _retry_after_seconds()
    if retry_after is None:
        return False
    try:
        budget = float(closure.router.RETRY_AFTER_MAX_SECONDS)
    except (TypeError, ValueError):
        return False
    return retry_after > max(0.0, budget)


def _install_rate_limit_ownership() -> None:
    """Make provider evidence authoritative without re-hitting a known-busy window."""
    router = closure.router
    if getattr(router, "_ISCO_RUN128_RATE_LIMIT_OWNERSHIP", False):
        return
    # Unit/import contexts can install the cache-prefix formatter without installing the
    # Run125 model pool. Rate-limit ownership only makes sense once that owner is live.
    if not closure._INSTALLED:
        return

    original_is_model_unavailable = closure._is_model_unavailable

    def model_unavailable_or_tpm_window(error) -> bool:
        return original_is_model_unavailable(error) or _is_tpm_window_exhausted(error)

    # groq_model_pool resolves this module global at call time. A real TPM 429 on 20b
    # therefore advances immediately to 120b instead of consuming the outer 20-second
    # retry cap and re-hitting the same minute window.
    closure._is_model_unavailable = model_unavailable_or_tpm_window

    original_groq_call = router._groq_call

    def groq_call_with_terminal_window_marker(prompt: str) -> dict:
        try:
            return original_groq_call(prompt)
        except Exception as exc:
            if _is_tpm_window_exhausted(exc):
                # No production model remains. Preserve exact provider reset evidence in
                # the marker already understood by Run124's bounded terminal owner.
                raise _terminal_tpm_window_error(exc) from None
            raise

    router._groq_call = groq_call_with_terminal_window_marker

    original_classify = router.classify_provider_failure

    def classify(provider_name: str, error):
        normalized = str(provider_name).strip().lower()
        lower = str(error).lower()

        if normalized.startswith("groq") and "groq_tpm_window_busy_precheck" in lower:
            return ProviderFailure(
                "capacity_wait",
                router.AttemptOutcome.RATE_LIMITED,
                False,
            )

        failure = original_classify(provider_name, error)

        # RETRY_AFTER_MAX_SECONDS is a latency budget, not permission to sleep only part
        # of the provider-mandated delay. If the delay is too large, fail over now.
        # Never issue a same-provider request before Retry-After has actually elapsed.
        if failure.telemetry_result == "429" and _retry_after_exceeds_local_budget():
            return ProviderFailure(
                "retry_after_exceeds_budget",
                router.AttemptOutcome.RATE_LIMITED,
                False,
            )
        return failure

    router.classify_provider_failure = classify
    router._ISCO_RUN128_RATE_LIMIT_OWNERSHIP = True


def _install_hard_tpd_classifier() -> None:
    router = closure.router
    if getattr(router, "_ISCO_RUN125_HARD_TPD_CLASSIFIER", False):
        return
    original = router.classify_provider_failure

    def classify(provider_name: str, error):
        if str(provider_name).startswith("groq") and closure._is_tpd_exhausted(error):
            return ProviderFailure(
                "quota_exhausted",
                router.AttemptOutcome.RATE_LIMITED,
                True,
            )
        return original(provider_name, error)

    router.classify_provider_failure = classify
    router._ISCO_RUN125_HARD_TPD_CLASSIFIER = True


def install_run125_cache_prefix_contract() -> None:
    # Qwen 3.8 is currently a preview model. Keep the production failover path on the
    # two GPT-OSS models that share strict structured output and prompt caching; a
    # preview model must not silently become a production dependency.
    closure._GROQ_MODEL_POOL = _PRODUCTION_GROQ_MODEL_POOL
    if closure._ACTIVE_GROQ_INDEX >= len(closure._GROQ_MODEL_POOL):
        closure._ACTIVE_GROQ_INDEX = 0
    closure._writer_cache_layout = _writer_cache_layout
    _install_hard_tpd_classifier()
    _install_rate_limit_ownership()
    print(
        "Run125 cache-prefix contract installed: "
        "writer_range_and_shard_state_after_shared_policy=true "
        "groq_production_pool=gpt-oss-20b->gpt-oss-120b hard_tpd_retry=false "
        "tpm_model_failover=true partial_retry_after=false terminal_reset_owner=run124"
    )
