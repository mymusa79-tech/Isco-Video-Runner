from __future__ import annotations

"""Transport hardening for Vision Stage Contract V2/V3.

The Stage Contract remains the semantic/schema/provider-policy owner. This module owns
only the raw HTTP boundary and the narrow composition adapters required by Run181:
- bind shared provider-health evidence to the existing run-scoped Vision circuit;
- preserve V2's TaskSpec provider-attempt budget when the V3 provider order is used;
- read Planning/Text-Audit evidence only from the current orchestrator.produce() call;
- canonicalize Gemini aliases through the pinned Engine provider before health matching;
- reuse Groq rate evidence only when it names the exact Qwen Vision model;
- classify a statically unavailable Groq credential as zero-inference readiness evidence.

No visual semantic rule, threshold, Security gate, candidate cap, or total inference
attempt ceiling is changed here.
"""

from contextvars import ContextVar
from dataclasses import replace
from functools import wraps

import requests

import isco_video_agent.orchestrator as orchestrator
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import task_level_planner_router as planner_router
from scripts import text_audit_provider_mesh as text_mesh
from scripts import vision_stage_contract_v2 as contract


# Planning and Text-Audit telemetry are intentionally append-only diagnostic logs.
# They may outlive one produce() call in a long-lived Python process. Capture their
# lengths at the real production-run boundary and import only the tail created by that
# run; otherwise an old provider failure could poison a later healthy run after Health
# itself was correctly reset.
_RUN181_TELEMETRY_BASELINE: ContextVar[tuple[int, int] | None] = ContextVar(
    "isco_run181_telemetry_baseline",
    default=None,
)


def _canonical_gemini_generation_model(model: object) -> str:
    """Resolve Engine compatibility aliases to the actual Gemini quota/model key."""
    raw = str(model or "").strip()
    resolver = getattr(contract.gemini_provider, "_content_model", None)
    if callable(resolver):
        try:
            resolved = str(resolver(raw)).strip()
            if resolved:
                return resolved
        except Exception:
            pass
    return raw


def _runtime_gemini_generation_model() -> str:
    return _canonical_gemini_generation_model(run181._gemini_runtime_model())


def _publish_current_gemini_unavailable(detail: object, *, source: str) -> None:
    if not run181._quota_or_rate_failure(detail):
        return
    health.publish_provider_unavailable(
        "gemini",
        model=_runtime_gemini_generation_model(),
        quota_domain=run181.GEMINI_GENERATION_QUOTA_DOMAIN,
        reason=str(detail),
        source=source,
    )


def _publish_exact_groq_vision_model_unavailable(
    model: object,
    detail: object,
    *,
    source: str,
) -> None:
    """Share only exact-model Groq rate/quota evidence with Vision.

    Groq Planning/Text Audit can use several models. A 20B/120B failure must not poison
    Qwen Vision, but a qwen/qwen3.8-27b rate failure is the same hosted model that Vision
    would call through the same chat-completions account boundary.
    """
    resolved = str(model or "").strip()
    if resolved != run181.GROQ_VISION_MODEL or not run181._quota_or_rate_failure(detail):
        return
    health.publish_provider_unavailable(
        "groq",
        model=run181.GROQ_VISION_MODEL,
        quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
        reason=str(detail),
        source=source,
    )


def _seed_static_groq_readiness() -> None:
    """Publish no-key evidence before V3 can spend a logical inference slot.

    A missing credential is known locally and causes no provider inference. V3 already
    knows how to skip a provider that has shared-health evidence and records that skip as
    CIRCUIT_OPEN, which the Engine BudgetLedger excludes from provider-attempt totals.
    Keeping this outside V3's local attempt increment preserves the pre-existing V2
    invariant: Gemini + two OpenRouter schema attempts remain possible when optional
    Groq is not configured.
    """
    existing = health.provider_unavailable(
        "groq",
        model=run181.GROQ_VISION_MODEL,
        quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
    )
    if existing is not None or run181._groq_key():
        return
    health.publish_provider_unavailable(
        "groq",
        model=run181.GROQ_VISION_MODEL,
        quota_domain=run181.GROQ_VISION_QUOTA_DOMAIN,
        reason="Groq key unavailable for Vision fallback",
        source="vision_static_readiness",
    )


def _classify_http(status: int, message: str) -> contract.VisionErrorCode:
    """Classify gateway HTTP failures by recovery scope, not by numeric code alone."""
    lowered = str(message or "").casefold()
    if status == 401:
        return contract.VisionErrorCode.AUTH_CONFIG
    if status == 403 and any(
        marker in lowered for marker in ("unauthorized", "api key", "invalid key", "permission")
    ):
        return contract.VisionErrorCode.AUTH_CONFIG
    if status in {402, 403}:
        return contract.VisionErrorCode.CAPACITY
    if status == 404 and any(
        marker in lowered for marker in ("provider", "endpoint", "parameter", "model")
    ):
        return contract.VisionErrorCode.CAPACITY
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return contract.VisionErrorCode.PROVIDER_TRANSIENT
    if status in {400, 404, 422} and any(
        marker in lowered for marker in ("schema", "parameter", "support", "modality")
    ):
        return contract.VisionErrorCode.CAPACITY
    return contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR


def _install_transport_boundary() -> None:
    contract._classify_http = _classify_http
    current = contract._openrouter_call
    if getattr(current, "_isco_vision_transport_v2", False):
        return

    @wraps(current)
    def guarded_openrouter_call(*args, **kwargs):
        requested_model = str(kwargs.get("model") or contract.OPENROUTER_PRIMARY_MODEL)
        try:
            return current(*args, **kwargs)
        except contract.VisionStageError:
            raise
        except requests.Timeout as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                "OpenRouter transport timeout",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc
        except requests.ConnectionError as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                "OpenRouter transport connection failure",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc
        except requests.RequestException as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.PROVIDER_TRANSIENT,
                f"OpenRouter transport request failure type={type(exc).__name__}",
                provider="openrouter",
                requested_model=requested_model,
            ) from exc

    guarded_openrouter_call._isco_vision_transport_v2 = True
    guarded_openrouter_call._isco_vision_transport_original = current
    contract._openrouter_call = guarded_openrouter_call


def _current_run_telemetry() -> tuple[list[dict], list[dict]]:
    planning = list(planner_router.get_telemetry())
    audit_routes = list(getattr(text_mesh, "_AUDIT_ROUTE_TELEMETRY", ()) or ())
    baseline = _RUN181_TELEMETRY_BASELINE.get()
    if baseline is None:
        return planning, audit_routes
    planning_start, audit_start = baseline
    planning_start = min(max(0, int(planning_start)), len(planning))
    audit_start = min(max(0, int(audit_start)), len(audit_routes))
    return planning[planning_start:], audit_routes[audit_start:]


def _scoped_refresh_runtime_provider_health() -> None:
    """Import hard evidence from this production run only, without replacing owners."""
    health.load_preflight_provider_health()
    planning_attempts, audit_routes = _current_run_telemetry()

    for attempt in planning_attempts:
        if not isinstance(attempt, dict):
            continue
        provider = str(attempt.get("provider") or "").strip().lower()
        result = str(attempt.get("result") or "").strip().lower()
        detail = attempt.get("error_detail")
        if provider == "gemini" and (
            result == "429" or run181._quota_or_rate_failure(detail)
        ):
            _publish_current_gemini_unavailable(
                detail or result,
                source="planning_telemetry",
            )
        elif provider == "groq" and (
            result == "429" or run181._quota_or_rate_failure(detail)
        ):
            _publish_exact_groq_vision_model_unavailable(
                attempt.get("resolved_model"),
                detail or result,
                source="planning_telemetry",
            )

    for route in audit_routes:
        if not isinstance(route, dict):
            continue
        for attempt in list(route.get("attempts") or ()):
            if not isinstance(attempt, dict):
                continue
            provider = str(attempt.get("provider") or "").strip().lower()
            outcome = str(attempt.get("outcome") or "").strip().lower()
            detail = attempt.get("detail")
            if provider == "gemini" and (
                outcome == "rate_limited" or run181._quota_or_rate_failure(detail)
            ):
                _publish_current_gemini_unavailable(
                    detail or outcome,
                    source="text_audit_telemetry",
                )
                continue
            if provider.startswith("groq:") and (
                outcome == "rate_limited" or run181._quota_or_rate_failure(detail)
            ):
                _publish_exact_groq_vision_model_unavailable(
                    provider[len("groq:"):],
                    detail or outcome,
                    source="text_audit_telemetry",
                )


def _install_run181_route_adapter() -> None:
    """Preserve V2 budget ownership while binding V3 health to the same run scope."""
    current = contract._route_visual_audit_v2
    if getattr(current, "_isco_run181_route_adapter", False):
        return

    @wraps(current)
    def run181_route_adapter(
        ledger,
        spec,
        provider: str,
        resolved_model: str,
        fn,
        *args,
        **kwargs,
    ):
        state = contract.legacy._state()
        if health.bind_provider_health_to_vision_scope(state):
            run181._GROQ_MODEL_CERTIFIED.set(None)
        _seed_static_groq_readiness()

        max_attempts = int(
            contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts
        )
        if max_attempts != 3:
            raise contract.VisionStageError(
                contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                f"Run181 route requires existing Vision attempt cap=3, found={max_attempts}",
                provider="internal",
            )

        canonical_model = _canonical_gemini_generation_model(resolved_model)
        routed_spec = replace(
            spec,
            max_provider_attempts=max(max_attempts, int(spec.max_provider_attempts)),
        )
        return current(
            ledger,
            routed_spec,
            provider,
            canonical_model,
            fn,
            *args,
            **kwargs,
        )

    run181_route_adapter._isco_run181_route_adapter = True
    run181_route_adapter._isco_run181_original = current
    contract._route_visual_audit_v2 = run181_route_adapter


def _install_run181_produce_telemetry_scope() -> None:
    """Capture append-only telemetry cursors at the actual production-run boundary."""
    current = orchestrator.produce
    if getattr(current, "_isco_run181_telemetry_scope", False):
        return

    @wraps(current)
    def scoped_produce(*args, **kwargs):
        existing = _RUN181_TELEMETRY_BASELINE.get()
        if existing is not None:
            return current(*args, **kwargs)
        baseline = (
            len(planner_router.get_telemetry()),
            len(getattr(text_mesh, "_AUDIT_ROUTE_TELEMETRY", ()) or ()),
        )
        token = _RUN181_TELEMETRY_BASELINE.set(baseline)
        try:
            return current(*args, **kwargs)
        finally:
            _RUN181_TELEMETRY_BASELINE.reset(token)

    scoped_produce._isco_run181_telemetry_scope = True
    scoped_produce._isco_run181_original = current
    orchestrator.produce = scoped_produce


def install_vision_provider_reliability() -> None:
    """Install HTTP hardening, Run181 mesh, budgets, run-scoped evidence, then V1 retrieval."""
    _install_transport_boundary()
    run181.install_run181_vision_mesh_closure()
    _install_run181_route_adapter()
    contract.install_vision_provider_reliability()
    run181.refresh_runtime_provider_health = _scoped_refresh_runtime_provider_health
    _install_run181_produce_telemetry_scope()
    # Run182 closure is deliberately composed after the provider mesh exists but before
    # the later Opening Feasibility wrapper. Its selector wrappers become the inner
    # semantic-preselection seam, while Opening Feasibility remains outermost owner of
    # query safety, review caps, and truthful technical-unavailable handling.
    from scripts.visual_retrieval_adjudication_v1 import install_visual_retrieval_adjudication_v1

    install_visual_retrieval_adjudication_v1()
