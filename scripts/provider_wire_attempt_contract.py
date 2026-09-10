from __future__ import annotations

"""Provider-attempt accounting contract for local pre-wire failures.

A Provider Attempt means an inference request crossed the provider wire boundary.
Local admission, credential/file validation, media preprocessing and frame extraction
are useful routing/preflight events but must not consume the provider-attempt budget or
poison provider health when no inference HTTP request was sent.

Run #238 exposed this first in Text Audit. The same accounting family also exists at
Vision boundaries where authorization can happen before local preview/frame work.
This module closes that family without changing provider order, quality thresholds,
semantic BLOCK behavior, or inference ceilings.
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
    return getattr(exc, "wire_attempted", None) is False


def _preview_key(preview: Path) -> str:
    try:
        return str(Path(preview).resolve())
    except Exception:
        return str(Path(preview))


def _install_frame_preprocessing_boundary() -> None:
    current = vision.legacy._sample_preview_frames
    if getattr(current, "_isco_no_wire_frame_preflight", False):
        return

    @wraps(current)
    def guarded_sample_preview_frames(preview: Path) -> list[bytes]:
        key = _preview_key(Path(preview))
        prepared = _PREPARED_FRAMES.get()
        if prepared is not None and key in prepared:
            # Return a shallow copy so downstream code cannot mutate the cached list.
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
        # The exception object is not available at this lower recording seam, so the
        # explicit no-wire reason marker is the durable proof transported here.
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
        # Keep deterministic local failures outside authorization. The underlying
        # function still repeats these checks for defense in depth, but they are now
        # proven before the provider-attempt budget can be consumed.
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
        # Reproduce the existing eligibility order exactly through the provider-owned
        # probes, then prepare frames before the local inference-slot reservation and
        # before BudgetLedger authorization. The actual _wire_call receives those same
        # frames from the ContextVar-backed sampler, so preprocessing cannot fail a
        # second time after reservation/authorization.
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
    """Install shared no-wire accounting before Vision/Gold runtime composition."""
    global _INSTALLED
    if _INSTALLED:
        return
    _install_frame_preprocessing_boundary()
    _install_wire_only_vision_recording()
    _install_openrouter_pre_authorization_local_checks()
    _install_cloudflare_preprocessing_before_reservation()
    _INSTALLED = True
    print(
        "Provider wire-attempt contract installed: local capacity/preprocessing=zero_attempts; "
        "Vision OpenRouter local checks=pre_authorization; Gold Cloudflare frames=pre_reservation"
    )
