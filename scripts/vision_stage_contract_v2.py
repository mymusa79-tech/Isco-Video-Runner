from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, replace
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any

import requests

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import AttemptOutcome, Capability, TaskSpec
from isco_video_agent.providers import gemini as gemini_provider
from scripts import vision_provider_reliability as legacy


VISION_STAGE_ID = "vision.visual_audit"
VISION_CONTRACT_ID = "vision.visual_audit.v2"
VISION_SEMANTIC_POLICY = "engine.visual_audit_normalizer.v1"
OPENROUTER_PRIMARY_MODEL = "openrouter/free"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models?input_modalities=image"
OPENROUTER_CATALOG_TIMEOUT_SECONDS = 15
OPENROUTER_MAX_MODEL_ATTEMPTS = 2

_VISUAL_PROPERTIES: dict[str, dict[str, Any]] = {
    "status": {"type": "string", "enum": ["pass", "block"]},
    "relevance": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "visual_quality": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "identifiable_person": {"type": "boolean"},
    "sensitive_trait_implication_risk": {"type": "boolean"},
    "prominent_logo_or_brand": {"type": "boolean"},
    "cultural_conflict": {"type": "boolean"},
    "cultural_islamic_suitability_risk": {"type": "boolean"},
    "advertiser_conflict": {"type": "boolean"},
    "obvious_synthetic_or_visual_artifact": {"type": "boolean"},
    "reason": {"type": "string"},
}
VISUAL_AUDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": _VISUAL_PROPERTIES,
    "required": list(_VISUAL_PROPERTIES),
    "additionalProperties": False,
}


class VisionErrorCode(str, Enum):
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    CAPACITY = "CAPACITY"
    STRUCTURAL_INVALID = "STRUCTURAL_INVALID"
    AUTH_CONFIG = "AUTH_CONFIG"
    INTERNAL_CONTRACT_ERROR = "INTERNAL_CONTRACT_ERROR"


class VisionStageError(RuntimeError):
    def __init__(
        self,
        code: VisionErrorCode,
        detail: str,
        *,
        provider: str | None = None,
        requested_model: str | None = None,
        resolved_model: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.provider = provider
        self.requested_model = requested_model
        self.resolved_model = resolved_model
        parts = [code.value, f"stage={VISION_STAGE_ID}"]
        if provider:
            parts.append(f"provider={provider}")
        if requested_model:
            parts.append(f"requested_model={requested_model}")
        if resolved_model:
            parts.append(f"resolved_model={resolved_model}")
        parts.append(detail)
        super().__init__(" ".join(parts))


@dataclass(frozen=True)
class VisionProviderPolicy:
    providers: tuple[str, ...] = ("gemini", "openrouter")
    max_total_inference_attempts: int = 3
    max_openrouter_model_attempts: int = OPENROUTER_MAX_MODEL_ATTEMPTS
    require_structured_outputs: bool = True
    require_provider_parameters: bool = True
    semantic_block_is_final: bool = True


@dataclass(frozen=True)
class VisionStageSpec:
    stage_id: str
    contract_id: str
    output_schema: dict[str, Any]
    semantic_policy: str
    provider_policy: VisionProviderPolicy


VISION_STAGE_SPEC = VisionStageSpec(
    stage_id=VISION_STAGE_ID,
    contract_id=VISION_CONTRACT_ID,
    output_schema=VISUAL_AUDIT_SCHEMA,
    semantic_policy=VISION_SEMANTIC_POLICY,
    provider_policy=VisionProviderPolicy(),
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vision_contract_fingerprint() -> str:
    payload = {
        "stage_id": VISION_STAGE_ID,
        "contract_id": VISION_CONTRACT_ID,
        "semantic_policy": VISION_SEMANTIC_POLICY,
        "output_schema": VISUAL_AUDIT_SCHEMA,
        "provider_policy": {
            "providers": list(VISION_STAGE_SPEC.provider_policy.providers),
            "max_total_inference_attempts": VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts,
            "max_openrouter_model_attempts": VISION_STAGE_SPEC.provider_policy.max_openrouter_model_attempts,
            "require_structured_outputs": True,
            "require_provider_parameters": True,
            "semantic_block_is_final": True,
        },
        "module_sha256": _sha256_file(Path(__file__).resolve()),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def vision_input_hash(preview: Path, *, narration_context: str, intended_visual: str) -> str:
    payload = {
        "contract": vision_contract_fingerprint(),
        "preview_sha256": _sha256_file(Path(preview)),
        "narration_context": str(narration_context),
        "intended_visual": str(intended_visual),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _openrouter_key() -> str:
    return legacy._openrouter_key()


def _strict_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "isco_visual_audit_v2",
            "strict": True,
            "schema": VISUAL_AUDIT_SCHEMA,
        },
    }


def _openrouter_request_payload(
    model: str,
    *,
    prompt: str,
    frame_items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}, *frame_items],
            }
        ],
        "temperature": 0,
        "max_tokens": 900,
        "response_format": _strict_response_format(),
        "provider": {
            "allow_fallbacks": True,
            "require_parameters": True,
        },
    }


def _schema_error(data: object, *, resolved_model: str | None = None) -> VisionStageError | None:
    if not isinstance(data, dict):
        return VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "visual response root must be an object",
            provider="openrouter",
            resolved_model=resolved_model,
        )
    expected = set(_VISUAL_PROPERTIES)
    actual = set(data)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing or extra:
        return VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            f"schema fields mismatch missing={missing} extra={extra}",
            provider="openrouter",
            resolved_model=resolved_model,
        )
    if data.get("status") not in {"pass", "block"}:
        return VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "status must be pass or block",
            provider="openrouter",
            resolved_model=resolved_model,
        )
    wrong_types: list[str] = []
    for key in ("relevance", "visual_quality"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            wrong_types.append(key)
        elif not 0.0 <= float(value) <= 1.0:
            return VisionStageError(
                VisionErrorCode.STRUCTURAL_INVALID,
                f"{key} out of range",
                provider="openrouter",
                resolved_model=resolved_model,
            )
    for key in legacy._BOOLEAN_VISUAL_KEYS:
        if not isinstance(data.get(key), bool):
            wrong_types.append(key)
    if not isinstance(data.get("reason"), str):
        wrong_types.append("reason")
    if wrong_types:
        return VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            f"schema type mismatch keys={sorted(set(wrong_types))}",
            provider="openrouter",
            resolved_model=resolved_model,
        )
    return None


def _parse_and_normalize(raw: object, *, resolved_model: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "visual response content is not text",
            provider="openrouter",
            resolved_model=resolved_model,
        )
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "visual response is not valid JSON",
            provider="openrouter",
            resolved_model=resolved_model,
        ) from exc
    error = _schema_error(data, resolved_model=resolved_model)
    if error is not None:
        raise error
    return gemini_provider._normalize_visual_audit(data)


def _extract_error_message(body: object) -> str:
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        return str(body["error"].get("message") or "").replace("\n", " ").strip()[:180]
    return ""


def _classify_http(status: int, message: str) -> VisionErrorCode:
    lowered = message.casefold()
    if status in {401, 403}:
        return VisionErrorCode.AUTH_CONFIG
    if status == 404 and ("provider" in lowered or "endpoint" in lowered or "parameter" in lowered):
        return VisionErrorCode.CAPACITY
    if status in {408, 409, 429, 500, 502, 503, 504}:
        return VisionErrorCode.PROVIDER_TRANSIENT
    if status in {400, 404, 422} and ("schema" in lowered or "parameter" in lowered or "support" in lowered):
        return VisionErrorCode.CAPACITY
    return VisionErrorCode.INTERNAL_CONTRACT_ERROR


def _openrouter_call(
    preview: Path,
    *,
    narration_context: str,
    intended_visual: str,
    model: str,
) -> tuple[dict[str, Any], str]:
    token = _openrouter_key()
    if not token:
        raise VisionStageError(
            VisionErrorCode.AUTH_CONFIG,
            "OpenRouter key unavailable",
            provider="openrouter",
            requested_model=model,
        )
    payload_bytes = Path(preview).read_bytes()
    if not payload_bytes or len(payload_bytes) > legacy.MAX_PREVIEW_BYTES:
        raise VisionStageError(
            VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "preview size is invalid before OpenRouter call",
            provider="openrouter",
            requested_model=model,
        )
    prompt = legacy._visual_prompt(
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
        for frame in legacy._sample_preview_frames(Path(preview))
    ]
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/mymusa79-tech/Isco-Video-Runner",
            "X-Title": "Isco Video Runner Vision Stage Contract V2",
            "X-OpenRouter-Metadata": "enabled",
        },
        json=_openrouter_request_payload(model, prompt=prompt, frame_items=frame_items),
        timeout=legacy.OPENROUTER_TIMEOUT_SECONDS,
    )
    try:
        body = response.json()
    except Exception as exc:
        raise VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID if response.ok else VisionErrorCode.PROVIDER_TRANSIENT,
            "OpenRouter response envelope is not valid JSON",
            provider="openrouter",
            requested_model=model,
        ) from exc
    if not response.ok:
        status = int(response.status_code)
        message = _extract_error_message(body)
        raise VisionStageError(
            _classify_http(status, message),
            f"HTTP_{status} message={message}",
            provider="openrouter",
            requested_model=model,
        )
    resolved = str(body.get("model") or model).strip()
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "OpenRouter response has no choice",
            provider="openrouter",
            requested_model=model,
            resolved_model=resolved,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise VisionStageError(
            VisionErrorCode.STRUCTURAL_INVALID,
            "OpenRouter response message missing",
            provider="openrouter",
            requested_model=model,
            resolved_model=resolved,
        )
    audit = _parse_and_normalize(message.get("content"), resolved_model=resolved)
    metadata = body.get("openrouter_metadata") if isinstance(body, dict) else None
    if isinstance(metadata, dict):
        route_attempt = metadata.get("attempt")
        strategy = str(metadata.get("strategy") or "")[:40]
        print(
            "Vision OpenRouter route metadata: "
            f"requested={model} resolved={resolved} strategy={strategy} attempt={route_attempt}"
        )
    return audit, resolved


def _price_is_zero(item: dict[str, Any]) -> bool:
    pricing = item.get("pricing")
    if not isinstance(pricing, dict):
        return str(item.get("id") or "").endswith(":free")
    for key in ("prompt", "completion"):
        value = pricing.get(key)
        if value is None:
            continue
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _discover_alternate_free_vision_model(*, exclude: set[str]) -> str | None:
    token = _openrouter_key()
    if not token:
        return None
    try:
        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers={"Authorization": "Bearer " + token},
            timeout=OPENROUTER_CATALOG_TIMEOUT_SECONDS,
        )
        if not response.ok:
            return None
        body = response.json()
    except Exception:
        return None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    candidates: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in exclude or not model_id.endswith(":free"):
            continue
        if not _price_is_zero(item):
            continue
        architecture = item.get("architecture")
        modalities = architecture.get("input_modalities") if isinstance(architecture, dict) else None
        if isinstance(modalities, list) and "image" not in modalities:
            continue
        parameters = item.get("supported_parameters")
        if isinstance(parameters, list) and "response_format" not in parameters:
            continue
        candidates.append(model_id)
    return sorted(candidates)[0] if candidates else None


def _attempt_outcome(exc: BaseException) -> AttemptOutcome:
    if isinstance(exc, VisionStageError):
        if exc.code is VisionErrorCode.STRUCTURAL_INVALID:
            return AttemptOutcome.SCHEMA_INVALID
        if exc.code is VisionErrorCode.PROVIDER_TRANSIENT:
            detail = exc.detail.casefold()
            if "429" in detail or "rate" in detail or "quota" in detail:
                return AttemptOutcome.RATE_LIMITED
            if "timeout" in detail:
                return AttemptOutcome.TIMEOUT
            return AttemptOutcome.OTHER
        return AttemptOutcome.OTHER
    return legacy._attempt_outcome(exc)


def _classify_gemini_failure(exc: BaseException) -> VisionErrorCode:
    detail = str(exc).casefold()
    name = type(exc).__name__.casefold()
    if "401" in detail or "403" in detail or "unauthorized" in detail or "permission" in detail:
        return VisionErrorCode.AUTH_CONFIG
    if (
        "429" in detail
        or "resource_exhausted" in detail
        or "quota" in detail
        or "rate limit" in detail
        or "rate_limit" in detail
        or "timeout" in name
        or "timeout" in detail
        or "timed out" in detail
        or "connection" in name
        or "connection" in detail
        or "network" in detail
        or any(code in detail for code in ("500", "502", "503", "504"))
    ):
        return VisionErrorCode.PROVIDER_TRANSIENT
    return VisionErrorCode.INTERNAL_CONTRACT_ERROR


def _record(
    ledger,
    spec: TaskSpec,
    *,
    provider: str,
    requested_model: str,
    resolved_model: str,
    outcome: AttemptOutcome,
    detail: str | None = None,
) -> None:
    legacy._record(
        ledger,
        spec,
        provider=provider,
        requested_model=requested_model,
        resolved_model=resolved_model,
        outcome=outcome,
        detail=detail,
    )


def _authorize(ledger, spec: TaskSpec) -> None:
    legacy._authorize(ledger, spec)


def _mesh_unavailable(state) -> legacy.VisionProviderMeshUnavailableError:
    return legacy._mesh_unavailable(state)


def _run_openrouter_attempt(
    ledger,
    spec: TaskSpec,
    *,
    preview: Path,
    narration_context: str,
    intended_visual: str,
    requested_model: str,
) -> tuple[dict[str, Any], str]:
    _authorize(ledger, spec)
    try:
        result, resolved = _openrouter_call(
            preview,
            narration_context=narration_context,
            intended_visual=intended_visual,
            model=requested_model,
        )
    except Exception as exc:
        resolved = getattr(exc, "resolved_model", None) or requested_model
        _record(
            ledger,
            spec,
            provider="openrouter",
            requested_model=requested_model,
            resolved_model=str(resolved),
            outcome=_attempt_outcome(exc),
            detail=legacy._safe_exception_detail(exc),
        )
        raise
    _record(
        ledger,
        spec,
        provider="openrouter",
        requested_model=requested_model,
        resolved_model=resolved,
        outcome=AttemptOutcome.CONTENT_BLOCKED if result.get("status") == "block" else AttemptOutcome.SUCCESS,
    )
    return result, resolved


def _route_visual_audit_v2(
    ledger,
    spec: TaskSpec,
    provider: str,
    resolved_model: str,
    fn,
    *args,
    **kwargs,
) -> dict[str, Any]:
    if spec.kind != "VISUAL_AUDIT" or spec.capability is not Capability.VISION or provider != "gemini":
        raise AssertionError("Vision Stage Contract V2 received a non-Visual task")
    if len(args) < 2:
        raise VisionStageError(
            VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "preview argument missing",
            provider="internal",
        )
    preview = Path(args[1])
    narration_context = str(kwargs.get("narration_context") or "")
    intended_visual = str(kwargs.get("intended_visual") or "")
    input_hash = vision_input_hash(
        preview,
        narration_context=narration_context,
        intended_visual=intended_visual,
    )

    routed_spec = replace(
        spec,
        max_provider_attempts=max(
            VISION_STAGE_SPEC.provider_policy.max_total_inference_attempts,
            spec.max_provider_attempts,
        ),
    )
    if ledger is not None:
        ledger.register_task(routed_spec)
    state = legacy._state()

    if not state.gemini_open:
        _authorize(ledger, routed_spec)
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            code = _classify_gemini_failure(exc)
            detail = legacy._safe_exception_detail(exc)
            _record(
                ledger,
                routed_spec,
                provider="gemini",
                requested_model=resolved_model,
                resolved_model=resolved_model,
                outcome=_attempt_outcome(exc),
                detail=detail,
            )
            if code in {VisionErrorCode.AUTH_CONFIG, VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.gemini_open = True
            state.gemini_reason = f"{code.value} {detail}"
            print(
                "Vision Stage Contract V2: Gemini circuit opened after technical failure; "
                f"input={input_hash[:12]} reason={state.gemini_reason}"
            )
        else:
            _record(
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
        _record(
            ledger,
            routed_spec,
            provider="gemini",
            requested_model=resolved_model,
            resolved_model=resolved_model,
            outcome=AttemptOutcome.CIRCUIT_OPEN,
            detail="run-scoped Gemini Vision circuit already open",
        )

    if state.openrouter_open:
        _record(
            ledger,
            routed_spec,
            provider="openrouter",
            requested_model=OPENROUTER_PRIMARY_MODEL,
            resolved_model="circuit-open",
            outcome=AttemptOutcome.CIRCUIT_OPEN,
            detail="run-scoped OpenRouter Vision circuit already open",
        )
        raise _mesh_unavailable(state)

    first_resolved: str | None = None
    try:
        result, first_resolved = _run_openrouter_attempt(
            ledger,
            routed_spec,
            preview=preview,
            narration_context=narration_context,
            intended_visual=intended_visual,
            requested_model=OPENROUTER_PRIMARY_MODEL,
        )
        return result
    except VisionStageError as first_error:
        if first_error.code is not VisionErrorCode.STRUCTURAL_INVALID:
            if first_error.code in {VisionErrorCode.AUTH_CONFIG, VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.openrouter_open = True
            state.openrouter_reason = legacy._safe_exception_detail(first_error)
            raise _mesh_unavailable(state) from first_error
        first_resolved = first_error.resolved_model or first_resolved
        excluded = {OPENROUTER_PRIMARY_MODEL}
        if first_resolved:
            excluded.add(first_resolved)
        alternate = _discover_alternate_free_vision_model(exclude=excluded)
        if not alternate:
            state.openrouter_open = True
            state.openrouter_reason = (
                f"{VisionErrorCode.STRUCTURAL_INVALID.value} no diverse free structured-vision model available "
                f"after {first_resolved or OPENROUTER_PRIMARY_MODEL}"
            )
            raise _mesh_unavailable(state) from first_error
        print(
            "Vision Stage Contract V2: schema-invalid OpenRouter response isolated to model; "
            f"retrying once with diverse free model={alternate} input={input_hash[:12]}"
        )
        try:
            result, _resolved = _run_openrouter_attempt(
                ledger,
                routed_spec,
                preview=preview,
                narration_context=narration_context,
                intended_visual=intended_visual,
                requested_model=alternate,
            )
            return result
        except VisionStageError as second_error:
            if second_error.code in {VisionErrorCode.AUTH_CONFIG, VisionErrorCode.INTERNAL_CONTRACT_ERROR}:
                raise
            state.openrouter_open = True
            state.openrouter_reason = legacy._safe_exception_detail(second_error)
            raise _mesh_unavailable(state) from second_error


def _bind_media_durable_audit_contract() -> None:
    try:
        from scripts import media_durable_cache as media_cache
    except Exception:
        return
    current = getattr(media_cache, "_audit_contract", None)
    if not callable(current) or getattr(current, "_isco_vision_stage_contract_v2", False):
        return

    @wraps(current)
    def bound_audit_contract() -> str:
        base = current()
        return media_cache._contract_hash(
            "audit-vision-stage-v2",
            base,
            vision_contract_fingerprint(),
        )

    bound_audit_contract._isco_vision_stage_contract_v2 = True
    bound_audit_contract._isco_vision_stage_contract_original = current
    media_cache._audit_contract = bound_audit_contract


def install_vision_provider_reliability() -> None:
    """Install one explicit Long+Short Vision Stage Contract V2 owner."""
    _bind_media_durable_audit_contract()

    current_call_status = orchestrator._ledger_call_status
    if not getattr(current_call_status, "_isco_vision_stage_contract_v2", False):
        @wraps(current_call_status)
        def routed_call_status(ledger, spec, provider, resolved_model, fn, *args, **kwargs):
            if (
                getattr(spec, "kind", "") == "VISUAL_AUDIT"
                and getattr(spec, "capability", None) is Capability.VISION
                and provider == "gemini"
            ):
                return _route_visual_audit_v2(
                    ledger,
                    spec,
                    provider,
                    resolved_model,
                    fn,
                    *args,
                    **kwargs,
                )
            return current_call_status(
                ledger,
                spec,
                provider,
                resolved_model,
                fn,
                *args,
                **kwargs,
            )

        routed_call_status._isco_shared_vision_provider_mesh = True
        routed_call_status._isco_vision_stage_contract_v2 = True
        routed_call_status._isco_shared_vision_original = current_call_status
        orchestrator._ledger_call_status = routed_call_status

    current_produce = orchestrator.produce
    if not getattr(current_produce, "_isco_vision_stage_contract_v2_scope", False):
        @wraps(current_produce)
        def scoped_produce(*args, **kwargs):
            with legacy.vision_provider_circuit_scope():
                return current_produce(*args, **kwargs)

        scoped_produce._isco_shared_vision_circuit_scope = True
        scoped_produce._isco_vision_stage_contract_v2_scope = True
        scoped_produce._isco_shared_vision_original = current_produce
        orchestrator.produce = scoped_produce

    print(
        "Vision Stage Contract V2 installed: shared Long+Short owner; Gemini primary; "
        "OpenRouter strict json_schema + require_parameters; one model-diverse schema recovery; "
        "semantic BLOCK final; run-scoped circuits; durable media-audit cache bound to V2 contract"
    )
