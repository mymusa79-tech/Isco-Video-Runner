from __future__ import annotations

"""Run #181 provider-health and Groq Vision family closure.

Run #181 proved two separate truths were available but not composed: Planning/Text
Audit knew the exact Gemini generation model was quota-limited, while provider preflight
already knew OpenRouter spend capacity was exhausted. Vision owned a private circuit and
therefore retried Gemini, then found its nominal fallback unavailable.

This module closes that family without weakening any visual gate:
- share only provider/model/quota-domain unavailability evidence across capabilities;
- seed provider-wide hard blocks from the existing zero-inference preflight artifact;
- add qwen/qwen3.8-27b as the bounded Groq Vision route between Gemini and OpenRouter;
- keep the existing exact visual schema, Engine normalizer/thresholds, semantic BLOCK
  finality, Security preflight, candidate caps, and total inference-attempt ceiling.
"""

import base64
import hashlib
import json
import os
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

import requests

from isco_video_agent.ai_budget import AttemptOutcome
from scripts import provider_health_registry as health
from scripts import task_level_planner_router as planner_router
from scripts import text_audit_provider_mesh as text_mesh
from scripts import vision_stage_contract_v2 as contract


GROQ_VISION_MODEL = "qwen/qwen3.8-27b"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TIMEOUT_SECONDS = 60
GROQ_CATALOG_TIMEOUT_SECONDS = 15
GEMINI_GENERATION_QUOTA_DOMAIN = "generate_content"
GROQ_VISION_QUOTA_DOMAIN = "vision"

_INSTALLED = False
_GROQ_MODEL_CERTIFIED: ContextVar[bool | None] = ContextVar(
    "isco_run181_groq_vision_model_certified",
    default=None,
)


def _gemini_runtime_model() -> str:
    return str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()


def _groq_key() -> str:
    direct = str(os.environ.get("GROQ_API_KEY") or "").strip()
    if direct:
        return direct
    file_name = str(os.environ.get("GROQ_API_KEY_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _quota_or_rate_failure(detail: object) -> bool:
    text = str(detail or "").casefold()
    return any(
        marker in text
        for marker in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted")
    )


def _publish_gemini_generation_unavailable(detail: object, *, source: str) -> None:
    if not _quota_or_rate_failure(detail):
        return
    health.publish_provider_unavailable(
        "gemini",
        model=_gemini_runtime_model(),
        quota_domain=GEMINI_GENERATION_QUOTA_DOMAIN,
        reason=str(detail),
        source=source,
    )


def _install_planning_health_bridge() -> None:
    current = planner_router._record_attempt
    if getattr(current, "_isco_run181_provider_health_bridge", False):
        return

    @wraps(current)
    def wrapped(provider_name: str, result: str, *args, **kwargs) -> None:
        current(provider_name, result, *args, **kwargs)
        if str(provider_name).lower() != "gemini":
            return
        detail = kwargs.get("error_detail")
        if str(result).lower() == "429" or _quota_or_rate_failure(detail):
            _publish_gemini_generation_unavailable(
                detail or result,
                source="planning_provider_loop",
            )

    wrapped._isco_run181_provider_health_bridge = True
    wrapped._isco_run181_original = current
    planner_router._record_attempt = wrapped


def _install_text_audit_health_bridge() -> None:
    current = text_mesh._record_route_telemetry
    if getattr(current, "_isco_run181_provider_health_bridge", False):
        return

    @wraps(current)
    def wrapped(route_result) -> None:
        current(route_result)
        for attempt in list(getattr(route_result, "attempts", ()) or ()):
            if str(getattr(attempt, "provider", "")).lower() != "gemini":
                continue
            outcome = getattr(attempt, "outcome", None)
            value = str(getattr(outcome, "value", outcome) or "").lower()
            detail = getattr(attempt, "detail", None)
            if value == "rate_limited" or _quota_or_rate_failure(detail):
                _publish_gemini_generation_unavailable(
                    detail or value,
                    source="text_audit_provider_mesh",
                )

    wrapped._isco_run181_provider_health_bridge = True
    wrapped._isco_run181_original = current
    text_mesh._record_route_telemetry = wrapped


def _certify_groq_vision_model() -> None:
    cached = _GROQ_MODEL_CERTIFIED.get()
    if cached is True:
        return
    if cached is False:
        evidence = health.provider_unavailable(
            "groq",
            model=GROQ_VISION_MODEL,
            quota_domain=GROQ_VISION_QUOTA_DOMAIN,
        )
        raise contract.VisionStageError(
            contract.VisionErrorCode.CAPACITY,
            evidence.reason if evidence else "Groq Vision model unavailable",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )

    token = _groq_key()
    if not token:
        raise contract.VisionStageError(
            contract.VisionErrorCode.AUTH_CONFIG,
            "Groq key unavailable for Vision fallback",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    try:
        response = requests.get(
            GROQ_MODELS_URL,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            timeout=GROQ_CATALOG_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            f"Groq model-catalog transport failure type={type(exc).__name__}",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    if not response.ok:
        code = contract._classify_http(int(response.status_code), "groq model catalog")
        raise contract.VisionStageError(
            code,
            f"Groq model catalog HTTP_{response.status_code}",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    try:
        body = response.json()
    except Exception as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Groq model catalog is not valid JSON",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    data = body.get("data") if isinstance(body, dict) else None
    ids = {
        str(item.get("id") or "").strip()
        for item in (data if isinstance(data, list) else [])
        if isinstance(item, dict)
    }
    if GROQ_VISION_MODEL not in ids:
        _GROQ_MODEL_CERTIFIED.set(False)
        health.publish_provider_unavailable(
            "groq",
            model=GROQ_VISION_MODEL,
            quota_domain=GROQ_VISION_QUOTA_DOMAIN,
            reason=f"Groq Vision model not found in live catalog: {GROQ_VISION_MODEL}",
            source="vision_catalog_preflight",
        )
        raise contract.VisionStageError(
            contract.VisionErrorCode.CAPACITY,
            f"Groq Vision model not found in live catalog: {GROQ_VISION_MODEL}",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    _GROQ_MODEL_CERTIFIED.set(True)


def _groq_parse_and_normalize(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Groq visual response content is not text",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Groq visual response is not valid JSON",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    error = contract._schema_error(data, resolved_model=GROQ_VISION_MODEL)
    if error is not None:
        raise contract.VisionStageError(
            error.code,
            error.detail,
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
            resolved_model=GROQ_VISION_MODEL,
        )
    return contract.gemini_provider._normalize_visual_audit(data)


def _groq_visual_call(
    preview: Path,
    *,
    narration_context: str,
    intended_visual: str,
) -> dict[str, Any]:
    _certify_groq_vision_model()
    token = _groq_key()
    prompt = contract.legacy._visual_prompt(
        narration_context=narration_context,
        intended_visual=intended_visual,
    )
    frame_items = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")
            },
        }
        for frame in contract.legacy._sample_preview_frames(Path(preview))
    ]
    payload = {
        "model": GROQ_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}, *frame_items],
            }
        ],
        "temperature": 0,
        "max_completion_tokens": 900,
        "reasoning_effort": "none",
        "include_reasoning": False,
        "response_format": contract._strict_response_format(),
    }
    try:
        response = requests.post(
            GROQ_CHAT_URL,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=payload,
            timeout=GROQ_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "Groq Vision transport timeout",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    except requests.RequestException as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            f"Groq Vision transport failure type={type(exc).__name__}",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID if response.ok else contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "Groq Vision response envelope is not valid JSON",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        ) from exc
    if not response.ok:
        message = contract._extract_error_message(body)
        raise contract.VisionStageError(
            contract._classify_http(int(response.status_code), message),
            f"HTTP_{response.status_code} message={message}",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Groq Vision response has no choice",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Groq Vision response message missing",
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
        )
    return _groq_parse_and_normalize(message.get("content"))


def _run_groq_attempt(ledger, spec, *, preview: Path, narration_context: str, intended_visual: str) -> dict[str, Any]:
    contract._authorize(ledger, spec)
    try:
        result = _groq_visual_call(
            preview,
            narration_context=narration_context,
            intended_visual=intended_visual,
        )
    except Exception as exc:
        contract._record(
            ledger,
            spec,
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
            resolved_model=GROQ_VISION_MODEL,
            outcome=contract._attempt_outcome(exc),
            detail=contract.legacy._safe_exception_detail(exc),
        )
        raise
    contract._record(
        ledger,
        spec,
        provider="groq",
        requested_model=GROQ_VISION_MODEL,
        resolved_model=GROQ_VISION_MODEL,
        outcome=AttemptOutcome.CONTENT_BLOCKED if result.get("status") == "block" else AttemptOutcome.SUCCESS,
    )
    return result


def _mesh_unavailable(state) -> contract.legacy.VisionProviderMeshUnavailableError:
    groq = health.provider_unavailable(
        "groq", model=GROQ_VISION_MODEL, quota_domain=GROQ_VISION_QUOTA_DOMAIN
    )
    reasons = [
        f"gemini={state.gemini_reason or 'unavailable'}",
        f"groq={(groq.reason if groq else 'unavailable')}",
        f"openrouter={state.openrouter_reason or 'unavailable'}",
    ]
    return contract.legacy.VisionProviderMeshUnavailableError(
        "Vision provider mesh unavailable: " + " | ".join(reasons)
    )


def _route_visual_audit_v3(
    ledger,
    spec,
    provider: str,
    resolved_model: str,
    fn,
    *args,
    **kwargs,
) -> dict[str, Any]:
    if spec.kind != "VISUAL_AUDIT" or spec.capability is not contract.Capability.VISION or provider != "gemini":
        raise AssertionError("Run181 Vision mesh received a non-Visual task")
    if len(args) < 2:
        raise contract.VisionStageError(
            contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "preview argument missing",
            provider="internal",
        )

    health.load_preflight_provider_health()
    preview = Path(args[1])
    narration_context = str(kwargs.get("narration_context") or "")
    intended_visual = str(kwargs.get("intended_visual") or "")
    input_hash = contract.vision_input_hash(
        preview,
        narration_context=narration_context,
        intended_visual=intended_visual,
    )
    routed_spec = replace(
        spec,
        max_provider_attempts=max(
            contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts,
            spec.max_provider_attempts,
        ),
    )
    if ledger is not None:
        ledger.register_task(routed_spec)
    state = contract.legacy._state()
    attempts = 0

    shared_gemini = health.provider_unavailable(
        "gemini",
        model=resolved_model,
        quota_domain=GEMINI_GENERATION_QUOTA_DOMAIN,
    )
    if shared_gemini is not None:
        state.gemini_open = True
        state.gemini_reason = f"shared_health:{shared_gemini.source}:{shared_gemini.reason}"

    if not state.gemini_open:
        contract._authorize(ledger, routed_spec)
        attempts += 1
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            code = contract._classify_gemini_failure(exc)
            detail = contract.legacy._safe_exception_detail(exc)
            contract._record(
                ledger,
                routed_spec,
                provider="gemini",
                requested_model=resolved_model,
                resolved_model=resolved_model,
                outcome=contract._attempt_outcome(exc),
                detail=detail,
            )
            if code in {contract.VisionErrorCode.AUTH_CONFIG, contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.gemini_open = True
            state.gemini_reason = f"{code.value} {detail}"
            _publish_gemini_generation_unavailable(detail, source="vision_stage")
            print(
                "Vision Stage Contract V3: Gemini circuit opened after technical failure; "
                f"input={input_hash[:12]} reason={state.gemini_reason}"
            )
        else:
            contract._record(
                ledger,
                routed_spec,
                provider="gemini",
                requested_model=resolved_model,
                resolved_model=resolved_model,
                outcome=(
                    AttemptOutcome.CONTENT_BLOCKED
                    if isinstance(result, dict) and result.get("status") == "block"
                    else AttemptOutcome.SUCCESS
                ),
            )
            return result
    else:
        contract._record(
            ledger,
            routed_spec,
            provider="gemini",
            requested_model=resolved_model,
            resolved_model=resolved_model,
            outcome=AttemptOutcome.CIRCUIT_OPEN,
            detail="run-scoped/shared Gemini generation circuit already open",
        )

    groq_evidence = health.provider_unavailable(
        "groq", model=GROQ_VISION_MODEL, quota_domain=GROQ_VISION_QUOTA_DOMAIN
    )
    if groq_evidence is None and attempts < contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts:
        attempts += 1
        try:
            result = _run_groq_attempt(
                ledger,
                routed_spec,
                preview=preview,
                narration_context=narration_context,
                intended_visual=intended_visual,
            )
            print(
                "Vision Stage Contract V3: Groq Vision fallback selected "
                f"model={GROQ_VISION_MODEL} input={input_hash[:12]}"
            )
            return result
        except contract.VisionStageError as exc:
            if exc.code in {contract.VisionErrorCode.AUTH_CONFIG, contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            health.publish_provider_unavailable(
                "groq",
                model=GROQ_VISION_MODEL,
                quota_domain=GROQ_VISION_QUOTA_DOMAIN,
                reason=contract.legacy._safe_exception_detail(exc),
                source="vision_stage",
            )
            print(
                "Vision Stage Contract V3: Groq Vision route unavailable; "
                f"input={input_hash[:12]} reason={contract.legacy._safe_exception_detail(exc)}"
            )
    elif groq_evidence is not None:
        contract._record(
            ledger,
            routed_spec,
            provider="groq",
            requested_model=GROQ_VISION_MODEL,
            resolved_model="circuit-open",
            outcome=AttemptOutcome.CIRCUIT_OPEN,
            detail=f"shared Groq Vision health unavailable source={groq_evidence.source}",
        )

    openrouter_evidence = health.provider_unavailable(
        "openrouter", model=contract.OPENROUTER_PRIMARY_MODEL, quota_domain="vision"
    )
    if openrouter_evidence is not None:
        state.openrouter_open = True
        state.openrouter_reason = f"shared_health:{openrouter_evidence.source}:{openrouter_evidence.reason}"

    if state.openrouter_open:
        contract._record(
            ledger,
            routed_spec,
            provider="openrouter",
            requested_model=contract.OPENROUTER_PRIMARY_MODEL,
            resolved_model="circuit-open",
            outcome=AttemptOutcome.CIRCUIT_OPEN,
            detail="run-scoped/shared OpenRouter circuit already open",
        )
        raise _mesh_unavailable(state)

    if attempts >= contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts:
        raise _mesh_unavailable(state)

    attempts += 1
    first_resolved: str | None = None
    try:
        result, first_resolved = contract._run_openrouter_attempt(
            ledger,
            routed_spec,
            preview=preview,
            narration_context=narration_context,
            intended_visual=intended_visual,
            requested_model=contract.OPENROUTER_PRIMARY_MODEL,
        )
        return result
    except contract.VisionStageError as first_error:
        if first_error.code is not contract.VisionErrorCode.STRUCTURAL_INVALID:
            if first_error.code in {contract.VisionErrorCode.AUTH_CONFIG, contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.openrouter_open = True
            state.openrouter_reason = contract.legacy._safe_exception_detail(first_error)
            health.publish_provider_unavailable(
                "openrouter",
                model="*",
                quota_domain="*",
                reason=state.openrouter_reason,
                source="vision_stage",
            )
            raise _mesh_unavailable(state) from first_error

        first_resolved = first_error.resolved_model or first_resolved
        # Preserve the existing one model-diverse schema recovery only when a bounded
        # inference slot remains. Gemini/Groq portability never expands the task cap.
        if attempts >= contract.VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts:
            state.openrouter_reason = contract.legacy._safe_exception_detail(first_error)
            raise _mesh_unavailable(state) from first_error
        excluded = {contract.OPENROUTER_PRIMARY_MODEL}
        if first_resolved:
            excluded.add(first_resolved)
        alternate = contract._discover_alternate_free_vision_model(exclude=excluded)
        if not alternate:
            state.openrouter_open = True
            state.openrouter_reason = (
                f"{contract.VisionErrorCode.STRUCTURAL_INVALID.value} no diverse free structured-vision model available "
                f"after {first_resolved or contract.OPENROUTER_PRIMARY_MODEL}"
            )
            raise _mesh_unavailable(state) from first_error
        attempts += 1
        print(
            "Vision Stage Contract V3: schema-invalid OpenRouter response isolated to model; "
            f"retrying once with diverse free model={alternate} input={input_hash[:12]}"
        )
        try:
            result, _resolved = contract._run_openrouter_attempt(
                ledger,
                routed_spec,
                preview=preview,
                narration_context=narration_context,
                intended_visual=intended_visual,
                requested_model=alternate,
            )
            return result
        except contract.VisionStageError as second_error:
            if second_error.code in {contract.VisionErrorCode.AUTH_CONFIG, contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.openrouter_open = True
            state.openrouter_reason = contract.legacy._safe_exception_detail(second_error)
            raise _mesh_unavailable(state) from second_error


def _install_fingerprint_binding() -> None:
    current = contract.vision_contract_fingerprint
    if getattr(current, "_isco_run181_vision_mesh_fingerprint", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "closure_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "groq_vision_model": GROQ_VISION_MODEL,
            "provider_order": ["gemini", "groq", "openrouter"],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    wrapped._isco_run181_vision_mesh_fingerprint = True
    wrapped._isco_run181_original = current
    contract.vision_contract_fingerprint = wrapped


def install_run181_vision_mesh_closure() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    health.load_preflight_provider_health()
    _install_planning_health_bridge()
    _install_text_audit_health_bridge()
    _install_fingerprint_binding()

    policy = replace(
        contract.VISION_STAGE_SPEC.provider_policy,
        providers=("gemini", "groq", "openrouter"),
        max_total_inference_attempts=3,
    )
    contract.VISION_STAGE_SPEC = replace(contract.VISION_STAGE_SPEC, provider_policy=policy)
    contract._route_visual_audit_v2 = _route_visual_audit_v3

    _INSTALLED = True
    print(
        "Run181 Vision mesh closure installed: provider_order=Gemini->Groq(qwen/qwen3.8-27b)->OpenRouter; "
        "shared provider/model/quota health=true; preflight blocks imported=true; total_inference_attempt_cap=3; "
        "visual schema/normalizer/semantic BLOCK/Security gates unchanged"
    )
