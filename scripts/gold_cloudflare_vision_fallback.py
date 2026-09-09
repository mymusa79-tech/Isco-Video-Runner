from __future__ import annotations

"""Gold-only fourth Vision provider using the existing Cloudflare account.

This route is deliberately narrower than the shared Run181 Vision mesh.  It is only
eligible after Gemini -> Groq -> OpenRouter are technically unavailable for the
GOLD_FINAL_CRITIC_OPENING_VISUAL task.  It never participates in normal production
Vision and it never authorizes a paid Cloudflare path.

Zero-cost safety is fail-closed:
* the existing Cloudflare token/account secrets must be present;
* the same token must prove that no active Workers Paid subscription exists;
* the token must prove Workers AI access through the model-schema endpoint;
* only the Cloudflare-hosted @cf/qwen/qwen3.8-27b model is allowed;
* one inference attempt maximum per Gold opening-Vision task;
* 403 paid-plan requirements, daily-free-allocation exhaustion and capacity errors
  are terminal provider-unavailable outcomes.  No billing/upgrade/AI-Gateway endpoint
  is ever called.
"""

import base64
import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from isco_video_agent.ai_budget import AttemptOutcome
from scripts import vision_stage_contract_v2 as contract


CLOUDFLARE_VISION_MODEL = "@cf/qwen/qwen3.8-27b"
CLOUDFLARE_VISION_PROVIDER = "cloudflare_workers_ai"
CLOUDFLARE_AI_QUOTA_DOMAIN = "gold_vision_free_only"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_TIMEOUT_SECONDS = 60
CLOUDFLARE_PROBE_TIMEOUT_SECONDS = 15
GOLD_OPENING_TASK_ID = "GOLD_FINAL_CRITIC_OPENING_VISUAL"

_ATTEMPTED: ContextVar[bool] = ContextVar("isco_gold_cloudflare_vision_attempted", default=False)


class CloudflareGoldVisionUnavailable(RuntimeError):
    pass


def _read_secret(direct_name: str, file_name: str) -> str:
    direct = str(os.environ.get(direct_name) or "").strip()
    if direct:
        return direct
    path = str(os.environ.get(file_name) or "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _credentials() -> tuple[str, str]:
    token = _read_secret("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN_FILE")
    account_id = _read_secret("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID_FILE")
    if not token or not account_id:
        raise CloudflareGoldVisionUnavailable("Cloudflare credentials are unavailable")
    if not all(ch.isalnum() for ch in account_id) or len(account_id) > 64:
        raise CloudflareGoldVisionUnavailable("Cloudflare account id is malformed")
    return token, account_id


def _enabled() -> bool:
    return str(os.environ.get("CLOUDFLARE_GOLD_VISION_FREE_ONLY") or "").strip().lower() == "true"


def _error_text(body: object) -> str:
    if not isinstance(body, dict):
        return "unknown Cloudflare API error"
    errors = body.get("errors")
    parts: list[str] = []
    if isinstance(errors, list):
        for item in errors[:3]:
            if isinstance(item, dict):
                code = item.get("code")
                message = str(item.get("message") or "").strip()
                parts.append(f"{code}:{message}" if code is not None else message)
    return " | ".join(part for part in parts if part) or str(body.get("message") or "unknown Cloudflare API error")


def _active_workers_paid_subscription(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    state = str(item.get("state") or "").strip().lower()
    if state in {"cancelled", "canceled", "expired", "failed"}:
        return False
    rate_plan = item.get("rate_plan")
    plan_id = ""
    public_name = ""
    if isinstance(rate_plan, dict):
        plan_id = str(rate_plan.get("id") or "").strip().upper()
        public_name = str(rate_plan.get("public_name") or "").strip().upper()
    if not plan_id:
        plan_id = str(item.get("rate_plan_id") or "").strip().upper()
    text = f"{plan_id} {public_name}"
    if "WORKERS" not in text:
        return False
    return not any(marker in text for marker in ("WORKERS_FREE", "PARTNERS_WORKERS_FREE"))


def _prove_workers_free(token: str, account_id: str) -> None:
    """Require billing-read evidence that this account cannot meter paid Workers AI.

    If the existing Telegram deploy token lacks Billing Read, the fourth provider is
    simply unavailable.  This is intentional: inability to prove zero-cost must never
    be converted into a potentially billable inference.
    """
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/subscriptions"
    try:
        response = requests.get(
            url,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            params={"per_page": 100},
            timeout=CLOUDFLARE_PROBE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise CloudflareGoldVisionUnavailable("Cloudflare free-plan proof timed out") from exc
    except requests.RequestException as exc:
        raise CloudflareGoldVisionUnavailable(
            f"Cloudflare free-plan proof transport failure type={type(exc).__name__}"
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise CloudflareGoldVisionUnavailable("Cloudflare free-plan proof returned non-JSON") from exc
    if not response.ok or not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare free-plan proof unavailable; Billing Read may be missing: " + _error_text(body)
        )
    result = body.get("result")
    if not isinstance(result, list):
        raise CloudflareGoldVisionUnavailable("Cloudflare subscription proof has invalid shape")
    if any(_active_workers_paid_subscription(item) for item in result):
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers Paid subscription detected; zero-cost Gold route disabled"
        )


def _prove_model_access(token: str, account_id: str) -> None:
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/ai/models/schema"
    try:
        response = requests.get(
            url,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            params={"model": CLOUDFLARE_VISION_MODEL},
            timeout=CLOUDFLARE_PROBE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise CloudflareGoldVisionUnavailable("Cloudflare Workers AI schema probe timed out") from exc
    except requests.RequestException as exc:
        raise CloudflareGoldVisionUnavailable(
            f"Cloudflare Workers AI schema probe transport failure type={type(exc).__name__}"
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise CloudflareGoldVisionUnavailable("Cloudflare Workers AI schema probe returned non-JSON") from exc
    if not response.ok or not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI permission/model probe failed: " + _error_text(body)
        )
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("input"), dict):
        raise CloudflareGoldVisionUnavailable("Cloudflare Workers AI model schema is invalid")


def _parse_normalized_response(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable("Cloudflare Workers AI inference failed: " + _error_text(body))
    result = body.get("result")
    raw: object = None
    if isinstance(result, dict):
        if "response" in result:
            raw = result.get("response")
        else:
            choices = result.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    raw = message.get("content")
    if isinstance(raw, str):
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError as exc:
            raise contract.VisionStageError(
                contract.VisionErrorCode.STRUCTURAL_INVALID,
                "Cloudflare Gold Vision response is not valid JSON",
                provider=CLOUDFLARE_VISION_PROVIDER,
                requested_model=CLOUDFLARE_VISION_MODEL,
            ) from exc
    elif isinstance(raw, dict):
        data = raw
    else:
        raise contract.VisionStageError(
            contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Cloudflare Gold Vision response content is missing",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        )
    error = contract._schema_error(data, resolved_model=CLOUDFLARE_VISION_MODEL)
    if error is not None:
        raise contract.VisionStageError(
            error.code,
            error.detail,
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
            resolved_model=CLOUDFLARE_VISION_MODEL,
        )
    return contract.gemini_provider._normalize_visual_audit(data)


def _wire_call(
    token: str,
    account_id: str,
    preview: Path,
    *,
    narration_context: str,
    intended_visual: str,
) -> dict[str, Any]:
    prompt = contract.legacy._visual_prompt(
        narration_context=narration_context,
        intended_visual=intended_visual,
    )
    frames = contract.legacy._sample_preview_frames(Path(preview))
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")},
        }
        for frame in frames
    )
    payload = {
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_completion_tokens": 700,
        "response_format": contract._strict_response_format(),
    }
    encoded_model = "/".join(quote(part, safe="@") for part in CLOUDFLARE_VISION_MODEL.split("/"))
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/ai/run/{encoded_model}"
    try:
        response = requests.post(
            url,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=payload,
            timeout=CLOUDFLARE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            "Cloudflare Gold Vision transport timeout",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        ) from exc
    except requests.RequestException as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT,
            f"Cloudflare Gold Vision transport failure type={type(exc).__name__}",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise contract.VisionStageError(
            contract.VisionErrorCode.PROVIDER_TRANSIENT if not response.ok else contract.VisionErrorCode.STRUCTURAL_INVALID,
            "Cloudflare Gold Vision response envelope is not valid JSON",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        ) from exc
    if not response.ok:
        detail = _error_text(body)
        # Cloudflare codes 3036 (free allocation exhausted), 3040 (capacity), and
        # 5035 (paid-plan-required) are provider availability outcomes.  We never
        # upgrade or purchase capacity; Gold remains QC_PENDING instead.
        raise contract.VisionStageError(
            contract._classify_http(int(response.status_code), detail),
            f"Cloudflare Workers AI HTTP_{response.status_code} {detail}",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        )
    return _parse_normalized_response(body)


def run_gold_cloudflare_attempt(
    ledger,
    spec,
    *,
    preview: Path,
    narration_context: str,
    intended_visual: str,
) -> dict[str, Any]:
    if getattr(spec, "task_id", "") != GOLD_OPENING_TASK_ID:
        raise CloudflareGoldVisionUnavailable("Cloudflare Vision is Gold-opening-only")
    if not _enabled():
        raise CloudflareGoldVisionUnavailable("Cloudflare zero-cost Gold Vision route is disabled")
    if _ATTEMPTED.get():
        raise CloudflareGoldVisionUnavailable("Cloudflare Gold Vision already attempted for this scope")

    token, account_id = _credentials()
    _prove_workers_free(token, account_id)
    _prove_model_access(token, account_id)

    _ATTEMPTED.set(True)
    contract._authorize(ledger, spec)
    try:
        result = _wire_call(
            token,
            account_id,
            Path(preview),
            narration_context=narration_context,
            intended_visual=intended_visual,
        )
    except Exception as exc:
        contract._record(
            ledger,
            spec,
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
            resolved_model=CLOUDFLARE_VISION_MODEL,
            outcome=contract._attempt_outcome(exc),
            detail=contract.legacy._safe_exception_detail(exc),
        )
        raise
    contract._record(
        ledger,
        spec,
        provider=CLOUDFLARE_VISION_PROVIDER,
        requested_model=CLOUDFLARE_VISION_MODEL,
        resolved_model=CLOUDFLARE_VISION_MODEL,
        outcome=(AttemptOutcome.CONTENT_BLOCKED if result.get("status") == "block" else AttemptOutcome.SUCCESS),
    )
    return result


def reset_attempt_scope() -> None:
    _ATTEMPTED.set(False)
