from __future__ import annotations

"""Mistral transport for bounded Clean V2 executor roles only.

This module is deliberately not a Gold/reviewer adapter.  Its public request
function accepts only the production executor roles explicitly approved here:
planning, script, and text_audit.  A future independent Gold judge must use a
separate transport and role boundary so Mistral cannot write and judge the same
content through this adapter.
"""

import hashlib
import json
import os
import socket
import urllib.error
import urllib.request
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping

MISTRAL_EXECUTOR_PROVIDER = "mistral"
MISTRAL_EXECUTOR_MODEL = "ministral-14b-2512"
MISTRAL_PLANNING_SCRIPT_MODEL = "mistral-small-2603"
MISTRAL_CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_TIMEOUT_SECONDS = 120
MISTRAL_EXECUTOR_TASKS = frozenset({"planning", "script", "text_audit"})
MAX_RESPONSE_BYTES = 20 * 1024 * 1024

_TELEMETRY: ContextVar[tuple[dict[str, Any], ...]] = ContextVar(
    "isco_clean_v2_mistral_executor_telemetry",
    default=(),
)


class MistralExecutorNoWireFailure(RuntimeError):
    wire_attempted = False

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "mistral_executor_local_rejection")
        super().__init__(self.reason_code)


class MistralExecutorWireFailure(RuntimeError):
    wire_attempted = True

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "mistral_executor_provider_failure")
        super().__init__(self.reason_code)


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


def mistral_executor_configured() -> bool:
    return bool(_read_secret("MISTRAL_API_KEY"))


def _model_for_task(task_kind: str) -> str:
    if task_kind in {"planning", "script"}:
        configured = str(os.environ.get("MISTRAL_CONTENT_MODEL") or "").strip()
        return configured or MISTRAL_PLANNING_SCRIPT_MODEL
    return MISTRAL_EXECUTOR_MODEL


def reset_mistral_executor_telemetry() -> None:
    _TELEMETRY.set(())


def get_mistral_executor_telemetry() -> list[dict[str, Any]]:
    return [dict(item) for item in _TELEMETRY.get()]


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
    task_kind: str,
    prompt: str,
    http_status: int,
    headers: Mapping[str, object],
    body: object,
) -> None:
    entry = {
        "provider": MISTRAL_EXECUTOR_PROVIDER,
        "role": "executor",
        "task_kind": task_kind,
        "model": _model_for_task(task_kind),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_utf8_bytes": len(prompt.encode("utf-8")),
        "http_status": int(http_status),
        "rate_limit_headers": _rate_limit_headers(headers),
        "usage": _usage(body),
    }
    _TELEMETRY.set((*_TELEMETRY.get(), entry))
    print(
        "Mistral Clean V2 executor telemetry: "
        + json.dumps(entry, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    )


def _response_format(
    response_schema: tuple[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    if response_schema is None:
        return {"type": "json_object"}
    name, schema = response_schema
    return {
        "type": "json_schema",
        "json_schema": {
            "name": str(name),
            "schema": schema,
            "strict": True,
        },
    }


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def mistral_executor_json(
    prompt: str,
    *,
    max_tokens: int,
    task_kind: str,
    response_schema: tuple[str, dict[str, Any]] | None = None,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Perform exactly one Mistral executor request and return one JSON object."""
    normalized_task = str(task_kind or "").strip().lower()
    if normalized_task not in MISTRAL_EXECUTOR_TASKS:
        raise MistralExecutorNoWireFailure("mistral_executor_role_not_allowed")
    token = _read_secret("MISTRAL_API_KEY")
    if not token:
        raise MistralExecutorNoWireFailure("missing_api_key")

    model = _model_for_task(normalized_task)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt
                + "\nReturn only one complete valid JSON object. No markdown.",
            }
        ],
        "response_format": _response_format(response_schema),
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    request = urllib.request.Request(
        MISTRAL_CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        ),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "Isco-Clean-V2-Mistral-Executor/1",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=MISTRAL_TIMEOUT_SECONDS,
        ) as response:
            http_status = int(getattr(response, "status", response.getcode()))
            response_headers = response.headers
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        http_status = int(exc.code)
        response_headers = exc.headers
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            body = None
        _record_telemetry(
            task_kind=normalized_task,
            prompt=prompt,
            http_status=http_status,
            headers=response_headers,
            body=body,
        )
        raise MistralExecutorWireFailure(f"http_{http_status}") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise MistralExecutorWireFailure(
            f"transport_{type(exc).__name__.lower()}"
        ) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise MistralExecutorWireFailure("response_too_large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _record_telemetry(
            task_kind=normalized_task,
            prompt=prompt,
            http_status=http_status,
            headers=response_headers,
            body=None,
        )
        raise MistralExecutorWireFailure("response_not_json") from None

    _record_telemetry(
        task_kind=normalized_task,
        prompt=prompt,
        http_status=http_status,
        headers=response_headers,
        body=body,
    )
    if not isinstance(body, dict):
        raise MistralExecutorWireFailure("response_not_object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise MistralExecutorWireFailure("mistral_no_choice")
    choice = choices[0]
    finish = str(choice.get("finish_reason") or "").strip().lower()
    if finish in {"length", "max_tokens"}:
        raise MistralExecutorWireFailure("mistral_output_truncated")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise MistralExecutorWireFailure("mistral_message_missing")
    raw = _message_text(message.get("content")).strip()
    if not raw:
        raise MistralExecutorWireFailure("mistral_empty_output")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise MistralExecutorWireFailure("mistral invalid json") from None
    if not isinstance(value, dict):
        raise MistralExecutorWireFailure("mistral_non_object")
    return value
