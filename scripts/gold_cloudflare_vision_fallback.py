from __future__ import annotations

"""Gold-only fourth Vision provider using the existing Cloudflare account.

This route is deliberately narrower than the shared Run181 Vision mesh. It is only
eligible after Gemini -> Groq -> OpenRouter are technically unavailable for the
GOLD_FINAL_CRITIC_OPENING_VISUAL task. It never participates in normal production
Vision and it never authorizes a paid Cloudflare path.

Zero-cost safety is fail-closed:
* the existing Cloudflare token/account secrets must be present;
* the same token must prove there is no active billable account subscription;
* the token must prove Workers AI access to the exact Cloudflare-hosted model;
* only @cf/google/gemma-4-26b-a4b-it is allowed; no paid/unified-billing route;
* one inference attempt maximum per Gold opening-Vision task;
* one workflow (Long plus sibling Shorts included) can reserve at most five calls;
* paid-plan requirements, daily-free-allocation exhaustion and capacity errors are
  provider-unavailable outcomes. No billing/upgrade/subscription/AI-Gateway write is
  ever performed.

Cloudflare's free Workers AI allocation is provider-owned capacity. When it is gone,
this adapter stops and lets the existing Gold QC_PENDING flow preserve Final Master.
"""

import base64
import fcntl
import json
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from isco_video_agent.ai_budget import AttemptOutcome
from scripts import vision_stage_contract_v2 as contract


CLOUDFLARE_VISION_MODEL = "@cf/google/gemma-4-26b-a4b-it"
CLOUDFLARE_VISION_PROVIDER = "cloudflare_workers_ai"
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_TIMEOUT_SECONDS = 60
CLOUDFLARE_PROBE_TIMEOUT_SECONDS = 15
CLOUDFLARE_MAX_CALLS_PER_WORKFLOW = 5
CLOUDFLARE_QUOTA_FILENAME = "cloudflare-gold-vision-quota-v1.json"
GOLD_OPENING_TASK_ID = "GOLD_FINAL_CRITIC_OPENING_VISUAL"

_ATTEMPTED: ContextVar[bool] = ContextVar(
    "isco_gold_cloudflare_vision_attempted",
    default=False,
)


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
    if len(account_id) != 32 or any(
        ch not in "0123456789abcdefABCDEF" for ch in account_id
    ):
        raise CloudflareGoldVisionUnavailable("Cloudflare account id is malformed")
    return token, account_id


def _enabled() -> bool:
    return (
        str(os.environ.get("CLOUDFLARE_GOLD_VISION_FREE_ONLY") or "")
        .strip()
        .lower()
        == "true"
    )


def _quota_path() -> Path:
    explicit = str(os.environ.get("CLOUDFLARE_GOLD_VISION_QUOTA_FILE") or "").strip()
    if explicit:
        return Path(explicit)
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare workflow quota state path is unavailable"
        )
    return Path(runner_temp) / CLOUDFLARE_QUOTA_FILENAME


def _max_calls_per_workflow() -> int:
    raw = str(
        os.environ.get("CLOUDFLARE_GOLD_VISION_MAX_CALLS_PER_WORKFLOW")
        or CLOUDFLARE_MAX_CALLS_PER_WORKFLOW
    ).strip()
    try:
        requested = int(raw)
    except ValueError as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare workflow call ceiling is malformed"
        ) from exc
    if requested < 1 or requested > CLOUDFLARE_MAX_CALLS_PER_WORKFLOW:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare workflow call ceiling must stay within 1..5"
        )
    return requested


def _reserve_workflow_call() -> None:
    """Reserve one inference slot shared by the parent and isolated Short children.

    GitHub's RUNNER_TEMP is inherited by the sequential sibling subprocesses, so this
    file lock caps a complete Long+Short bundle rather than each Python process alone.
    Separate workflow runs are still bounded by Workers Free's provider-enforced daily
    allocation; this local ceiling prevents one approved bundle from consuming more
    than the agreed five Gold fallback calls.
    """
    try:
        path = _quota_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        maximum = _max_calls_per_workflow()
        today = datetime.now(timezone.utc).date().isoformat()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            os.chmod(path, 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            raw = handle.read().strip()
            if raw:
                try:
                    state = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise CloudflareGoldVisionUnavailable(
                        "Cloudflare workflow quota state is invalid"
                    ) from exc
                if not isinstance(state, dict):
                    raise CloudflareGoldVisionUnavailable(
                        "Cloudflare workflow quota state is invalid"
                    )
            else:
                state = {}
            count = state.get("count", 0) if state.get("utc_date") == today else 0
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise CloudflareGoldVisionUnavailable(
                    "Cloudflare workflow quota count is invalid"
                )
            if count >= maximum:
                raise CloudflareGoldVisionUnavailable(
                    f"Cloudflare workflow call ceiling exhausted ({maximum})"
                )
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "schema_version": 1,
                    "utc_date": today,
                    "count": count + 1,
                    "maximum": maximum,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except CloudflareGoldVisionUnavailable:
        raise
    except OSError as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare workflow quota state is unavailable"
        ) from exc


def _error_parts(body: object) -> tuple[int | None, str]:
    if not isinstance(body, dict):
        return None, "unknown Cloudflare API error"
    errors = body.get("errors")
    parts: list[str] = []
    first_code: int | None = None
    if isinstance(errors, list):
        for item in errors[:3]:
            if not isinstance(item, dict):
                continue
            raw_code = item.get("code")
            code: int | None = None
            try:
                code = int(raw_code) if raw_code is not None else None
            except (TypeError, ValueError):
                code = None
            if first_code is None and code is not None:
                first_code = code
            message = str(item.get("message") or "").replace("\n", " ").strip()[:180]
            parts.append(f"{code}:{message}" if code is not None else message)
    detail = " | ".join(part for part in parts if part)
    if not detail:
        detail = str(body.get("message") or "unknown Cloudflare API error")[:180]
    return first_code, detail


def _error_text(body: object) -> str:
    return _error_parts(body)[1]


def _subscription_is_active(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    state = str(item.get("state") or "").strip().lower()
    return state not in {"cancelled", "canceled", "expired", "failed"}


def _subscription_is_proven_free(item: object) -> bool:
    """Accept only account subscription evidence that cannot create usage charges.

    The Cloudflare account-subscription API is broader than Workers. We therefore use
    a deliberately conservative rule: an active subscription must have a zero price
    and an explicit free rate-plan id. Any paid/unknown active subscription disables
    the fourth provider rather than guessing that Workers AI overage cannot bill.
    """
    if not isinstance(item, dict):
        return False
    if not _subscription_is_active(item):
        return True
    price = item.get("price")
    try:
        if price is not None and float(price) != 0.0:
            return False
    except (TypeError, ValueError):
        return False
    rate_plan = item.get("rate_plan")
    if not isinstance(rate_plan, dict):
        return False
    plan_id = str(rate_plan.get("id") or "").strip().lower()
    return plan_id in {"free", "partners_free"}


def _prove_workers_free(token: str, account_id: str) -> None:
    """Require Billing Read evidence before any Workers AI inference.

    If the existing Telegram deploy token lacks Billing Read, the fourth provider is
    simply unavailable. This is intentional: inability to prove zero-cost must never
    be converted into a potentially billable request.
    """
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/subscriptions"
    try:
        response = requests.get(
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            params={"per_page": 100},
            timeout=CLOUDFLARE_PROBE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare zero-cost subscription proof timed out"
        ) from exc
    except requests.RequestException as exc:
        raise CloudflareGoldVisionUnavailable(
            f"Cloudflare zero-cost subscription proof transport failure type={type(exc).__name__}"
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare zero-cost subscription proof returned non-JSON"
        ) from exc
    if not response.ok or not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare zero-cost subscription proof unavailable; Billing Read may be missing: "
            + _error_text(body)
        )
    result = body.get("result")
    if not isinstance(result, list):
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare subscription proof has invalid shape"
        )
    unproven = [
        item for item in result
        if _subscription_is_active(item) and not _subscription_is_proven_free(item)
    ]
    if unproven:
        raise CloudflareGoldVisionUnavailable(
            "Active paid/unknown Cloudflare subscription detected; zero-cost Gold route disabled"
        )


def _prove_model_access(token: str, account_id: str) -> None:
    """Read the exact model schema; this performs no inference and no billing write."""
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/ai/models/schema"
    try:
        response = requests.get(
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            params={"model": CLOUDFLARE_VISION_MODEL},
            timeout=CLOUDFLARE_PROBE_TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI schema probe timed out"
        ) from exc
    except requests.RequestException as exc:
        raise CloudflareGoldVisionUnavailable(
            f"Cloudflare Workers AI schema probe transport failure type={type(exc).__name__}"
        ) from exc
    try:
        body = response.json()
    except Exception as exc:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI schema probe returned non-JSON"
        ) from exc
    if not response.ok or not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI permission/model probe failed: " + _error_text(body)
        )
    result = body.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("input"), dict):
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI model schema is invalid"
        )


def _parse_normalized_response(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or body.get("success") is not True:
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Workers AI inference failed: " + _error_text(body)
        )
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


def _cloudflare_http_code(status: int, body: object) -> contract.VisionErrorCode:
    internal_code, detail = _error_parts(body)
    if internal_code in {3036, 5035}:
        # Daily free allocation exhausted or model requires Paid. Never buy/upgrade.
        return contract.VisionErrorCode.CAPACITY
    if internal_code == 3040:
        return contract.VisionErrorCode.PROVIDER_TRANSIENT
    if internal_code == 5016:
        # Model agreement required: do not accept a third-party license automatically.
        return contract.VisionErrorCode.AUTH_CONFIG
    return contract._classify_http(status, detail)


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
    # Gemma 4's model card recommends placing images before text for multimodal
    # understanding. Keep the same three bounded frames and unchanged Gold schema.
    content: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")
            },
        }
        for frame in frames
    ]
    content.append({"type": "text", "text": prompt})
    payload = {
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_completion_tokens": 700,
        "service_tier": "default",
        "store": False,
        "response_format": contract._strict_response_format(),
    }
    encoded_model = "/".join(
        quote(part, safe="@") for part in CLOUDFLARE_VISION_MODEL.split("/")
    )
    url = f"{CLOUDFLARE_API_BASE}/accounts/{account_id}/ai/run/{encoded_model}"
    try:
        response = requests.post(
            url,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
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
            (
                contract.VisionErrorCode.PROVIDER_TRANSIENT
                if not response.ok
                else contract.VisionErrorCode.STRUCTURAL_INVALID
            ),
            "Cloudflare Gold Vision response envelope is not valid JSON",
            provider=CLOUDFLARE_VISION_PROVIDER,
            requested_model=CLOUDFLARE_VISION_MODEL,
        ) from exc
    if not response.ok:
        detail = _error_text(body)
        raise contract.VisionStageError(
            _cloudflare_http_code(int(response.status_code), body),
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
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare zero-cost Gold Vision route is disabled"
        )
    if _ATTEMPTED.get():
        raise CloudflareGoldVisionUnavailable(
            "Cloudflare Gold Vision already attempted for this scope"
        )

    # Mark the scope before network preflights so a failed eligibility probe cannot be
    # retried blindly and turn a provider outage into additional delay.
    _ATTEMPTED.set(True)

    token, account_id = _credentials()
    _prove_workers_free(token, account_id)
    _prove_model_access(token, account_id)

    _reserve_workflow_call()
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
        outcome=(
            AttemptOutcome.CONTENT_BLOCKED
            if result.get("status") == "block"
            else AttemptOutcome.SUCCESS
        ),
    )
    return result


def reset_attempt_scope() -> None:
    _ATTEMPTED.set(False)
