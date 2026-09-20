from __future__ import annotations

"""Run a tiny, text-only Mistral availability probe.

The probe deliberately bypasses the production pipeline.  It compares the
configured account's response for the content model with a known control model
using the same API key, and records only bounded, non-secret diagnostics.
"""

import argparse
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


MISTRAL_API_BASE = "https://api.mistral.ai/v1"
TARGET_MODEL = "mistral-small-2603"
CONTROL_MODEL = "ministral-14b-2512"
MAX_RESPONSE_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 45


def _bounded(value: object, limit: int = 500) -> str | None:
    if value is None:
        return None
    return str(value).strip()[:limit] or None


def _rate_limit_headers(headers: Mapping[str, object] | None) -> dict[str, str]:
    if headers is None:
        return {}
    captured: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).strip().lower()
        if name == "retry-after" or name.startswith("x-ratelimit-"):
            captured[name] = str(raw_value).strip()[:300]
    return dict(sorted(captured.items()))


def _decode_body(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _safe_error(body: object) -> dict[str, str | None] | None:
    if not isinstance(body, dict):
        return None
    source = body.get("error") if isinstance(body.get("error"), dict) else body
    if not isinstance(source, dict):
        return None
    result = {
        name: _bounded(source.get(name))
        for name in ("object", "type", "message", "code", "param")
    }
    return result if any(value is not None for value in result.values()) else None


def _request(
    token: str,
    *,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int | None, Mapping[str, object] | None, object, str | None]:
    data = None
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/json",
        "User-Agent": "Isco-Mistral-Text-Entitlement-Probe/1",
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        MISTRAL_API_BASE + path,
        data=data,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", response.getcode()))
            response_headers = response.headers
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        response_headers = exc.headers
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        return None, None, None, type(exc).__name__.lower()

    if len(raw) > MAX_RESPONSE_BYTES:
        return status, response_headers, None, "response_too_large"
    return status, response_headers, _decode_body(raw), None


def catalog_presence(token: str) -> dict[str, Any]:
    status, headers, body, transport_error = _request(
        token,
        method="GET",
        path="/models",
    )
    model_ids: set[str] = set()
    if isinstance(body, dict) and isinstance(body.get("data"), list):
        for item in body["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                model_ids.add(item["id"])
    return {
        "http_status": status,
        "transport_error": transport_error,
        "rate_limit_headers": _rate_limit_headers(headers),
        "listed_model_count": len(model_ids),
        "target_listed": TARGET_MODEL in model_ids,
        "control_listed": CONTROL_MODEL in model_ids,
        "error": _safe_error(body),
    }


def probe_model(token: str, model: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": 'Return exactly this JSON object: {"probe":"ok"}',
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 16,
        "stream": False,
    }
    status, headers, body, transport_error = _request(
        token,
        method="POST",
        path="/chat/completions",
        payload=payload,
    )
    response_model = None
    finish_reason = None
    usage = None
    if isinstance(body, dict):
        response_model = _bounded(body.get("model"), 100)
        if isinstance(body.get("usage"), dict):
            usage = {
                str(name): value
                for name, value in body["usage"].items()
                if isinstance(value, (bool, int, float, str)) or value is None
            }
        choices = body.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = _bounded(choices[0].get("finish_reason"), 100)
    return {
        "requested_model": model,
        "request_kind": "text_only_chat_completion",
        "prompt_utf8_bytes": len(
            'Return exactly this JSON object: {"probe":"ok"}'.encode("utf-8")
        ),
        "max_tokens": 16,
        "http_status": status,
        "transport_error": transport_error,
        "rate_limit_headers": _rate_limit_headers(headers),
        "response_model": response_model,
        "finish_reason": finish_reason,
        "usage": usage,
        "error": _safe_error(body),
    }


def _request_minute_limit(result: Mapping[str, Any]) -> float | None:
    headers = result.get("rate_limit_headers")
    if not isinstance(headers, dict):
        return None
    preferred = (
        "x-ratelimit-limit-req-minute",
        "x-ratelimit-limit-requests-minute",
    )
    candidates = [headers.get(name) for name in preferred]
    candidates.extend(
        value
        for name, value in headers.items()
        if "limit" in name and "req" in name and "minute" in name
    )
    for value in candidates:
        try:
            return float(str(value))
        except (TypeError, ValueError):
            continue
    return None


def classify(target: Mapping[str, Any], control: Mapping[str, Any]) -> dict[str, Any]:
    target_status = target.get("http_status")
    control_status = control.get("http_status")
    target_limit = _request_minute_limit(target)
    retry_after = bool(
        isinstance(target.get("rate_limit_headers"), dict)
        and target["rate_limit_headers"].get("retry-after")
    )

    if target_status == 200:
        return {
            "outcome": "target_available_now",
            "conclusion": "mistral-small-2603 accepted a live text request",
            "decisive": True,
        }
    if target_status == 429 and target_limit == 0 and control_status == 200:
        return {
            "outcome": "target_zero_rpm_for_account",
            "conclusion": (
                "mistral-small-2603 has a zero request-per-minute entitlement "
                "for this account while the control model works under the same key"
            ),
            "decisive": True,
        }
    if target_status in {400, 403, 404}:
        return {
            "outcome": "target_model_rejected_or_unavailable",
            "conclusion": "the API rejected the exact target model for this account",
            "decisive": True,
        }
    if target_status == 429 and target_limit is not None and target_limit > 0:
        return {
            "outcome": "target_temporarily_rate_limited",
            "conclusion": (
                "the target has a positive configured RPM limit but no capacity now"
            ),
            "decisive": True,
            "retry_after_present": retry_after,
        }
    if target_status == 429 and control_status == 429:
        return {
            "outcome": "account_or_temporal_capacity_exhausted",
            "conclusion": "both target and control were rate limited; retry after reset",
            "decisive": False,
        }
    if target_status == 429 and target_limit == 0:
        return {
            "outcome": "target_zero_rpm_control_inconclusive",
            "conclusion": (
                "the target reports zero RPM, but the control did not prove usable capacity"
            ),
            "decisive": False,
        }
    return {
        "outcome": "inconclusive",
        "conclusion": "the paired responses do not distinguish entitlement from capacity",
        "decisive": False,
    }


def run_probe(token: str, *, ambiguous_retry_seconds: int = 65) -> dict[str, Any]:
    catalog = catalog_presence(token)
    rounds: list[dict[str, Any]] = []

    target = probe_model(token, TARGET_MODEL)
    control = probe_model(token, CONTROL_MODEL)
    verdict = classify(target, control)
    rounds.append({"round": 1, "target": target, "control": control, "verdict": verdict})

    if not verdict["decisive"] and ambiguous_retry_seconds > 0:
        time.sleep(ambiguous_retry_seconds)
        target = probe_model(token, TARGET_MODEL)
        control = probe_model(token, CONTROL_MODEL)
        verdict = classify(target, control)
        rounds.append(
            {"round": 2, "target": target, "control": control, "verdict": verdict}
        )

    return {
        "schema_version": 1,
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "probe_kind": "isolated_text_only_no_production",
        "production_attempt_started": False,
        "target_model": TARGET_MODEL,
        "control_model": CONTROL_MODEL,
        "catalog": catalog,
        "rounds": rounds,
        "final_classification": verdict,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ambiguous-retry-seconds", type=int, default=65)
    args = parser.parse_args()

    token = str(os.environ.get("MISTRAL_API_KEY") or "").strip()
    if not token:
        raise SystemExit("MISTRAL_API_KEY is required")
    result = run_probe(
        token,
        ambiguous_retry_seconds=max(0, args.ambiguous_retry_seconds),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "probe": "complete",
                "production_attempt_started": False,
                "target_model": TARGET_MODEL,
                "control_model": CONTROL_MODEL,
                "classification": result["final_classification"],
                "round_count": len(result["rounds"]),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
