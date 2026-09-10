from __future__ import annotations

"""Provider-attempt accounting contract for local pre-wire failures.

A Provider Attempt means an inference request crossed the provider wire boundary.
Local admission, credential/file validation, media preprocessing and frame extraction
are useful routing/preflight events but must not consume the provider-attempt budget or
poison provider health when no inference HTTP request was sent.

Run #238 exposed this first in Text Audit. The same accounting family can occur in any
wrapper that authorizes before invoking a provider callable. This module therefore
applies one fail-closed invariant to Planning, direct-provider calls, shared Vision and
Gold Cloudflare: only an exception carrying explicit ``wire_attempted=False`` proof is
excluded from provider-attempt accounting. Unknown exceptions remain counted.
"""

from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from scripts import gold_cloudflare_vision_fallback as cloudflare_gold
from scripts import vision_stage_contract_v2 as vision


NO_WIRE_MARKER = "NO_WIRE_LOCAL_FAILURE"
_PREPARED_FRAMES: ContextVar[dict[str, list[bytes]] | None] = ContextVar(
    "isco_no_wire_prepared_vision_frames",
    default=None,
)
_INSTALLED = False


class NoWireVisionStageError(vision.VisionStageError):
    """A Vision-stage failure proven to have happened before inference transport."""

    wire_attempted = False
    reason_code = NO_WIRE_MARKER

    def __init__(
        self,
        code: vision.VisionErrorCode,
        detail: str,
        *,
        provider: str | None = None,
        requested_model: str | None = None,
        resolved_model: str | None = None,
    ) -> None:
        super().__init__(
            code,
            f"{NO_WIRE_MARKER} {detail}",
            provider=provider,
            requested_model=requested_model,
            resolved_model=resolved_model,
        )


def is_no_wire_failure(exc: BaseException) -> bool:
    """No inference-attempt exemption without explicit proof from the boundary owner."""
    return getattr(exc, "wire_attempted", None) is False


def _preview_key(preview: Path) -> str:
    try:
        return str(Path(preview).resolve())
    except Exception:
        return str(Path(preview))


def _install_planning_budget_boundary() -> None:
    """Future-proof Planning if a local pre-wire check moves inside its budget wrapper."""
    from scripts import task_level_planner_router as planner

    current = planner._budgeted_provider_call
    if getattr(current, "_isco_wire_only_provider_attempts", False):
        return

    @wraps(current)
    def wire_only_budgeted_provider_call(provider_name: str, resolved_model: str, call, *args, **kwargs):
        active = planner.get_active_budget_task()
        if active is None:
            return call(*args, **kwargs)
        if not active.ledger.authorize(active.spec.task_id):
            raise RuntimeError(
                f"AI budget authorization denied for task {active.spec.task_id}; provider call blocked"
            )
        started = planner.time.monotonic()
        try:
            result = call(*args, **kwargs)
        except Exception as exc:
            if not is_no_wire_failure(exc):
                failure = planner.classify_provider_failure(provider_name, exc)
                planner._record_budget_attempt(
                    provider_name,
                    resolved_model,
                    failure.budget_outcome,
                    duration_seconds=planner.time.monotonic() - started,
                    detail=str(exc)[:220],
                )
            raise
        planner._record_budget_attempt(
            provider_name,
            resolved_model,
            planner.AttemptOutcome.SUCCESS,
            duration_seconds=planner.time.monotonic() - started,
        )
        return result

    wire_only_budgeted_provider_call._isco_wire_only_provider_attempts = True
    wire_only_budgeted_provider_call._isco_wire_only_original = current
    planner._budgeted_provider_call = wire_only_budgeted_provider_call


def _install_direct_provider_budget_boundary() -> None:
    """Apply the same proof rule to Engine direct Vision/TTS/provider wrappers."""
    import isco_video_agent.orchestrator as orchestrator

    current_call = orchestrator._ledger_call
    if not getattr(current_call, "_isco_wire_only_provider_attempts", False):
        @wraps(current_call)
        def wire_only_call(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            orchestrator._ledger_authorize(ledger, spec)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if not is_no_wire_failure(exc):
                    orchestrator._ledger_record(
                        ledger,
                        spec.task_id,
                        provider=provider,
                        resolved_model=resolved_model,
                        capability=spec.capability,
                        outcome=orchestrator._classify_exception(exc),
                    )
                raise
            orchestrator._ledger_record(
                ledger,
                spec.task_id,
                provider=provider,
                resolved_model=resolved_model,
                capability=spec.capability,
                outcome=orchestrator.AttemptOutcome.SUCCESS,
            )
            return result

        wire_only_call._isco_wire_only_provider_attempts = True
        wire_only_call._isco_wire_only_original = current_call
        orchestrator._ledger_call = wire_only_call

    current_status = orchestrator._ledger_call_status
    if not getattr(current_status, "_isco_wire_only_provider_attempts", False):
        @wraps(current_status)
        def wire_only_status(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            orchestrator._ledger_authorize(ledger, spec)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                if not is_no_wire_failure(exc):
                    orchestrator._ledger_record(
                        ledger,
                        spec.task_id,
                        provider=provider,
                        resolved_model=resolved_model,
                        capability=spec.capability,
                        outcome=orchestrator._classify_exception(exc),
                    )
                raise
            outcome = (
                orchestrator.AttemptOutcome.CONTENT_BLOCKED
                if result.get("status") == "block"
                else orchestrator.AttemptOutcome.SUCCESS
            )
            orchestrator._ledger_record(
                ledger,
                spec.task_id,
                provider=provider,
                resolved_model=resolved_model,
                capability=spec.capability,
                outcome=outcome,
            )
            return result

        wire_only_status._isco_wire_only_provider_attempts = True
        wire_only_status._isco_wire_only_original = current_status
        orchestrator._ledger_call_status = wire_only_status


def _install_gemini_vision_local_marker() -> None:
    """Mark deterministic Gemini visual-preflight failures before the Vision router sees them."""
    import isco_video_agent.orchestrator as orchestrator

    current = orchestrator.audit_video_preview
    if getattr(current, "_isco_no_wire_gemini_visual_preflight", False):
        return

    @wraps(current)
    def guarded_gemini_visual_audit(api_key, preview: Path, *args, **kwargs):
        # Prove the candidate bytes exist/read locally before Vision authorizes Gemini.
        try:
            Path(preview).read_bytes()
        except Exception as exc:
            raise NoWireVisionStageError(
                vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                f"Gemini preview read failed before inference type={type(exc).__name__}",
                provider="local_preflight",
                requested_model=str(kwargs.get("model") or "gemini"),
            ) from exc
        try:
            return current(api_key, Path(preview), *args, **kwargs)
        except Exception as exc:
            detail = str(exc)
            # These two failures are deterministic local setup/request-size failures in
            # the pinned Engine provider and occur before interactions.create().
            if (
                "Visual review preview exceeds Gemini inline total-request safety budget" in detail
                or "google-genai is required for Gemini production" in detail
            ):
                raise NoWireVisionStageError(
                    vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                    f"Gemini local visual preflight failed: {detail[:180]}",
                    provider="local_preflight",
                    requested_model=str(kwargs.get("model") or "gemini"),
                ) from exc
            raise

    guarded_gemini_visual_audit._isco_no_wire_gemini_visual_preflight = True
    guarded_gemini_visual_audit._isco_no_wire_original = current
    orchestrator.audit_video_preview = guarded_gemini_visual_audit


def _install_frame_preprocessing_boundary() -> None:
    current = vision.legacy._sample_preview_frames
    if getattr(current, "_isco_no_wire_frame_preflight", False):
        return

    @wraps(current)
    def guarded_sample_preview_frames(preview: Path) -> list[bytes]:
        key = _preview_key(Path(preview))
        prepared = _PREPARED_FRAMES.get()
        if prepared is not None and key in prepared:
            return list(prepared[key])
        try:
            return current(Path(preview))
        except NoWireVisionStageError:
            raise
        except Exception as exc:
            raise NoWireVisionStageError(
                vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                f"Vision frame preprocessing failed type={type(exc).__name__}",
                provider="local_preflight",
            ) from exc

    guarded_sample_preview_frames._isco_no_wire_frame_preflight = True
    guarded_sample_preview_frames._isco_no_wire_original = current
    vision.legacy._sample_preview_frames = guarded_sample_preview_frames


def _install_wire_only_vision_recording() -> None:
    current = vision._record
    if getattr(current, "_isco_wire_only_provider_attempts", False):
        return

    @wraps(current)
    def wire_only_record(
        ledger,
        spec,
        *,
        provider: str,
        requested_model: str,
        resolved_model: str,
        outcome,
        detail: str | None = None,
    ) -> None:
        if detail is not None and NO_WIRE_MARKER in str(detail):
            return
        return current(
            ledger,
            spec,
            provider=provider,
            requested_model=requested_model,
            resolved_model=resolved_model,
            outcome=outcome,
            detail=detail,
        )

    wire_only_record._isco_wire_only_provider_attempts = True
    wire_only_record._isco_wire_only_original = current
    vision._record = wire_only_record


def _install_openrouter_pre_authorization_local_checks() -> None:
    current = vision._run_openrouter_attempt
    if getattr(current, "_isco_no_wire_openrouter_preflight", False):
        return
    transport_owner = vision._openrouter_call

    @wraps(current)
    def guarded_openrouter_attempt(
        ledger,
        spec,
        *,
        preview: Path,
        narration_context: str,
        intended_visual: str,
        requested_model: str,
    ):
        # The captured transport is the only boundary this wrapper can prove no-wire for.
        # If another component replaces that seam later, it owns its own preflight proof;
        # unknown replacement failures still flow through the existing fail-closed counter.
        if vision._openrouter_call is transport_owner:
            if not vision._openrouter_key():
                raise NoWireVisionStageError(
                    vision.VisionErrorCode.AUTH_CONFIG,
                    "OpenRouter key unavailable before inference",
                    provider="openrouter",
                    requested_model=requested_model,
                )
            try:
                payload_bytes = Path(preview).read_bytes()
            except Exception as exc:
                raise NoWireVisionStageError(
                    vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                    f"preview read failed before OpenRouter inference type={type(exc).__name__}",
                    provider="local_preflight",
                    requested_model=requested_model,
                ) from exc
            if not payload_bytes or len(payload_bytes) > vision.legacy.MAX_PREVIEW_BYTES:
                raise NoWireVisionStageError(
                    vision.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
                    "preview size is invalid before OpenRouter inference",
                    provider="local_preflight",
                    requested_model=requested_model,
                )
        return current(
            ledger,
            spec,
            preview=Path(preview),
            narration_context=narration_context,
            intended_visual=intended_visual,
            requested_model=requested_model,
        )

    guarded_openrouter_attempt._isco_no_wire_openrouter_preflight = True
    guarded_openrouter_attempt._isco_no_wire_original = current
    vision._run_openrouter_attempt = guarded_openrouter_attempt


def _install_cloudflare_preprocessing_before_reservation() -> None:
    current = cloudflare_gold.run_gold_cloudflare_attempt
    if getattr(current, "_isco_no_wire_cloudflare_preflight", False):
        return

    @wraps(current)
    def guarded_cloudflare_attempt(
        ledger,
        spec,
        *,
        preview: Path,
        narration_context: str,
        intended_visual: str,
    ):
        if getattr(spec, "task_id", "") != cloudflare_gold.GOLD_OPENING_TASK_ID:
            raise cloudflare_gold.CloudflareGoldVisionUnavailable(
                "Cloudflare Vision is Gold-opening-only"
            )
        if not cloudflare_gold._enabled():
            raise cloudflare_gold.CloudflareGoldVisionUnavailable(
                "Cloudflare zero-cost Gold Vision route is disabled"
            )
        if cloudflare_gold._ATTEMPTED.get():
            raise cloudflare_gold.CloudflareGoldVisionUnavailable(
                "Cloudflare Gold Vision already attempted for this scope"
            )

        cloudflare_gold._ATTEMPTED.set(True)
        token, account_id = cloudflare_gold._credentials()
        cloudflare_gold._prove_workers_free(token, account_id)
        cloudflare_gold._prove_model_access(token, account_id)

        # Prepare once before both the local workflow inference-slot reservation and
        # BudgetLedger authorization. _wire_call's second sample access is served from
        # the ContextVar cache, so it cannot fail later after those counters advance.
        frames = vision.legacy._sample_preview_frames(Path(preview))
        key = _preview_key(Path(preview))
        frame_token = _PREPARED_FRAMES.set({key: list(frames)})
        try:
            cloudflare_gold._reserve_workflow_call()
            vision._authorize(ledger, spec)
            try:
                result = cloudflare_gold._wire_call(
                    token,
                    account_id,
                    Path(preview),
                    narration_context=narration_context,
                    intended_visual=intended_visual,
                )
            except Exception as exc:
                vision._record(
                    ledger,
                    spec,
                    provider=cloudflare_gold.CLOUDFLARE_VISION_PROVIDER,
                    requested_model=cloudflare_gold.CLOUDFLARE_VISION_MODEL,
                    resolved_model=cloudflare_gold.CLOUDFLARE_VISION_MODEL,
                    outcome=vision._attempt_outcome(exc),
                    detail=vision.legacy._safe_exception_detail(exc),
                )
                raise
            vision._record(
                ledger,
                spec,
                provider=cloudflare_gold.CLOUDFLARE_VISION_PROVIDER,
                requested_model=cloudflare_gold.CLOUDFLARE_VISION_MODEL,
                resolved_model=cloudflare_gold.CLOUDFLARE_VISION_MODEL,
                outcome=(
                    cloudflare_gold.AttemptOutcome.CONTENT_BLOCKED
                    if result.get("status") == "block"
                    else cloudflare_gold.AttemptOutcome.SUCCESS
                ),
            )
            return result
        finally:
            _PREPARED_FRAMES.reset(frame_token)

    guarded_cloudflare_attempt._isco_no_wire_cloudflare_preflight = True
    guarded_cloudflare_attempt._isco_no_wire_original = current
    cloudflare_gold.run_gold_cloudflare_attempt = guarded_cloudflare_attempt


def install_provider_wire_attempt_contract() -> None:
    """Install the no-wire invariant before provider-specific runtime composition."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_planning_budget_boundary()
    _install_direct_provider_budget_boundary()
    _install_gemini_vision_local_marker()
    _install_frame_preprocessing_boundary()
    _install_wire_only_vision_recording()
    _install_openrouter_pre_authorization_local_checks()
    _install_cloudflare_preprocessing_before_reservation()
    _INSTALLED = True
    print(
        "Provider wire-attempt contract installed: explicit no-wire proof=zero attempts; "
        "Planning/direct wrappers hardened; Vision local checks=pre-authorize; "
        "Gold Cloudflare frames=pre-reservation"
    )
