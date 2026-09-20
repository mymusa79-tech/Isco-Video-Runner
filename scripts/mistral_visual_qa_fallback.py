from __future__ import annotations

"""Final, Visual-Audit-only Mistral fallback.

The adapter accepts only Canonical Visual Evidence V1. It sends the same three
original-source frames and the same canonical prompt used by the other Visual QA
providers, enforces the existing Visual Audit JSON Schema, and delegates the final
semantic normalization to the Engine. It owns no threshold or semantic policy.
"""

import base64
import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping

import requests

from isco_video_agent.ai_budget import AttemptOutcome
from scripts import canonical_visual_evidence_v1 as canonical_evidence
from scripts import vision_stage_contract_v2 as contract


MISTRAL_VISION_PROVIDER = "mistral"
MISTRAL_VISION_MODEL = "ministral-14b-2512"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_TIMEOUT_SECONDS = 60
MISTRAL_VISION_QUOTA_DOMAIN = "vision"

_TELEMETRY: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "isco_mistral_visual_qa_telemetry",
    default=(),
)


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    file_name = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def mistral_visual_configured() -> bool:
    return bool(_read_secret("MISTRAL_API_KEY"))


def reset_mistral_visual_qa_telemetry() -> None:
    _TELEMETRY.set(())


def get_mistral_visual_qa_telemetry() -> list[dict[str, Any]]:
    return [dict(item) for item in _TELEMETRY.get()]


def latest_retry_after_seconds() -> float | None:
    """Return the exact Retry-After seconds observed on the latest Mistral response."""
    entries = _TELEMETRY.get()
    if not entries:
        return None
    headers = entries[-1].get("rate_limit_headers")
    if not isinstance(headers, dict):
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0.0 else None


def _rate_limit_headers(headers: Mapping[str, object]) -> dict[str, str]:
    captured: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name == "retry-after" or name.startswith("x-ratelimit-"):
            captured[name] = str(raw_value).strip()[:300]
    return dict(sorted(captured.items()))


def _usage(body: object) -> dict[str, Any] | None:
    if not isinstance(body, dict) or not isinstance(body.get("usage"), dict):
        return None
    usage: dict[str, Any] = {}
    for raw_name, value in body["usage"].items():
        name = str(raw_name)
        if isinstance(value, bool) or value is None:
            usage[name] = value
        elif isinstance(value, (int, float, str)):
            usage[name] = value
    return usage or None


def _record_telemetry(
    *,
    response: requests.Response,
    body: object,
) -> None:
    entry = {
        "provider": MISTRAL_VISION_PROVIDER,
        "model": MISTRAL_VISION_MODEL,
        "http_status": int(response.status_code),
        "rate_limit_headers": _rate_limit_headers(response.headers),
        "usage": _usage(body),
    }
    _TELEMETRY.set((*_TELEMETRY.get(), entry))
    print(
        "Mistral Visual QA telemetry: "
        + json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _mistral_image_content(
    evidence: canonical_evidence.CanonicalVisualEvidence,
) -> list[dict[str, Any]]:
    evidence = canonical_evidence.require_canonical_evidence(evidence)
    return [
        {
            "type": "image_url",
            "image_url": "data:image/jpeg;base64,"
            + base64.b64encode(frame).decode("ascii"),
        }
        for frame in evidence.frame_bytes()
    ]


def _request_payload(
    evidence: canonical_evidence.CanonicalVisualEvidence,
) -> dict[str, Any]:
    evidence = canonical_evidence.require_canonical_evidence(evidence)
    return {
        "model": MISTRAL_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    *_mistral_image_content(evidence),
                    {"type": "text", "text": evidence.prompt},
                ],
            }
        ],
        "temperature": 0,
        "max_tokens": 900,
        "response_format": contract._strict_response_format(),
    }


def _parse_and_normalize(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Mistral Visual QA response content is not text",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
        )
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Mistral Visual QA response is not valid JSON",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
        ) from exc
    error = contract._schema_error(data, resolved_model=MISTRAL_VISION_MODEL)
    if error is not None:
        raise contract.VisionStageError(
            error.code,
            error.detail,
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
        )
    return contract.gemini_provider._normalize_visual_audit(data)


def _mistral_visual_call(
    preview: Path,
    *,
    narration_context: str,
    intended_visual: str,
    canonical_visual_evidence: canonical_evidence.CanonicalVisualEvidence | None,
) -> dict[str, Any]:
    del preview, narration_context, intended_visual
    if canonical_visual_evidence is None:
        raise contract.VisionStageError(
            contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "Mistral Visual QA requires Canonical Visual Evidence V1",
            provider="internal",
            requested_model=MISTRAL_VISION_MODEL,
        )
    evidence = canonical_evidence.require_canonical_evidence(canonical_visual_evidence)
    token = _read_secret("MISTRAL_API_KEY")
    if not token:
        raise contract.VisionStageError(
            contract.VisionErrorCode.AUTH_CONFIG,
            "Mistral API key unavailable for Visual QA fallback",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
        )
    try:
        response = requests.post(
            MISTRAL_CHAT_URL,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            json=_request_payload(evidence),
            timeout=MISTRAL_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "Mistral Visual QA transport timeout",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
        ) from exc
    except requests.RequestException as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            f"Mistral Visual QA transport failure type={type(exc).__name__}",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
        ) from exc

    try:
        body = response.json()
    except Exception as exc:
        _record_telemetry(response=response, body=None)
        raise contract.VisionStageError(
            (
                contract.VisionErrorCode.STRUCTURAL_INVALID
                if response.ok
                else contract.VisionErrorCode.PROVIDER_TRANSIENT
            ),
            "Mistral Visual QA response envelope is not valid JSON",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            http_status=int(response.status_code),
        ) from exc

    _record_telemetry(response=response, body=body)
    if not response.ok:
        message = contract._extract_error_message(body)
        status = int(response.status_code)
        raise contract.VisionStageError(
            contract._classify_http(status, message),
            f"HTTP_{status} message={message}",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            http_status=status,
            http_message=message,
        )
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Mistral Visual QA response has no choice",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
        )
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Mistral Visual QA response message missing",
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
        )
    return _parse_and_normalize(message.get("content"))


def run_mistral_visual_qa_attempt(
    ledger,
    spec,
    *,
    preview: Path,
    narration_context: str,
    intended_visual: str,
    canonical_visual_evidence: canonical_evidence.CanonicalVisualEvidence | None,
) -> dict[str, Any]:
    if getattr(spec, "kind", "") != "VISUAL_AUDIT":
        raise contract.VisionStageError(
            contract.VisionErrorCode.INTERNAL_CONTRACT_ERROR,
            "Mistral fallback is Visual-Audit-only",
            provider="internal",
            requested_model=MISTRAL_VISION_MODEL,
        )
    contract._authorize(ledger, spec)
    try:
        result = _mistral_visual_call(
            Path(preview),
            narration_context=narration_context,
            intended_visual=intended_visual,
            canonical_visual_evidence=canonical_visual_evidence,
        )
    except Exception as exc:
        contract._record(
            ledger,
            spec,
            provider=MISTRAL_VISION_PROVIDER,
            requested_model=MISTRAL_VISION_MODEL,
            resolved_model=MISTRAL_VISION_MODEL,
            outcome=contract._attempt_outcome(exc),
            detail=contract.legacy._safe_exception_detail(exc),
        )
        raise
    contract._record(
        ledger,
        spec,
        provider=MISTRAL_VISION_PROVIDER,
        requested_model=MISTRAL_VISION_MODEL,
        resolved_model=MISTRAL_VISION_MODEL,
        outcome=(
            AttemptOutcome.CONTENT_BLOCKED
            if result.get("status") == "block"
            else AttemptOutcome.SUCCESS
        ),
    )
    return result
