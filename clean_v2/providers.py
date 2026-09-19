from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


MAX_PROMPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024


class NoWireFailure(RuntimeError):
    """A local rejection that is proven to happen before an HTTP request."""

    wire_attempted = False

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "local_rejection")
        super().__init__(self.reason_code)


class ProviderWireFailure(RuntimeError):
    wire_attempted = True

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "provider_failure")
        super().__init__(self.reason_code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    file_value = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not file_value:
        return ""
    path = Path(file_value)
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Isco-Clean-V2/1",
            **headers,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ProviderWireFailure(f"http_{int(exc.code)}") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise ProviderWireFailure(f"transport_{type(exc).__name__.lower()}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProviderWireFailure("response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProviderWireFailure("response_not_json") from None
    if not isinstance(value, dict):
        raise ProviderWireFailure("response_not_object")
    return value


def _parse_json_object(raw: str, provider: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        value = None
        for index, character in enumerate(text):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise ProviderWireFailure(f"{provider}_invalid_json") from None
    if not isinstance(value, dict):
        raise ProviderWireFailure(f"{provider}_non_object")
    return value


def _gemini_call(prompt: str, max_tokens: int) -> dict[str, Any]:
    key = _read_secret("GEMINI_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
    if not model:
        raise NoWireFailure("missing_model")
    body = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": key},
        payload={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": int(max_tokens),
                "responseMimeType": "application/json",
            },
        },
        timeout=90,
    )
    candidates = body.get("candidates") or []
    if not candidates or not isinstance(candidates[0], dict):
        raise ProviderWireFailure("gemini_no_candidate")
    finish = str(candidates[0].get("finishReason") or "").strip().upper()
    if finish in {"MAX_TOKENS", "MALFORMED_FUNCTION_CALL"}:
        raise ProviderWireFailure(f"gemini_finish_{finish.lower()}")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") if isinstance(content, dict) else None
    raw = "".join(
        str(item.get("text") or "")
        for item in (parts or [])
        if isinstance(item, dict)
    )
    return _parse_json_object(raw, "gemini")


def _groq_call(prompt: str, max_tokens: int) -> dict[str, Any]:
    key = _read_secret("GROQ_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("GROQ_CONTENT_MODEL") or "openai/gpt-oss-20b").strip()
    if not model:
        raise NoWireFailure("missing_model")
    body = _post_json(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        payload={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt + "\nReturn only one complete JSON object. No markdown.",
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "max_completion_tokens": int(max_tokens),
        },
        timeout=90,
    )
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderWireFailure("groq_no_choice")
    finish = str(choices[0].get("finish_reason") or "").strip().lower()
    if finish in {"length", "max_tokens"}:
        raise ProviderWireFailure("groq_output_truncated")
    message = choices[0].get("message") or {}
    return _parse_json_object(str(message.get("content") or ""), "groq")


def _openrouter_call(prompt: str, max_tokens: int) -> dict[str, Any]:
    key = _read_secret("OPENROUTER_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("OPENROUTER_CONTENT_MODEL") or "openai/gpt-oss-20b:free").strip()
    if model != "openai/gpt-oss-20b:free":
        raise NoWireFailure("paid_or_unapproved_model")
    body = _post_json(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/mymusa79-tech/Isco-Video-Runner",
            "X-Title": "Isco Clean V2",
        },
        payload={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt + "\nReturn only one complete JSON object. No markdown.",
                }
            ],
            "response_format": {"type": "json_object"},
            "provider": {"allow_fallbacks": True},
            "temperature": 0.3,
            "max_tokens": int(max_tokens),
        },
        timeout=120,
    )
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderWireFailure("openrouter_no_choice")
    message = choices[0].get("message") or {}
    return _parse_json_object(str(message.get("content") or ""), "openrouter")


@dataclass(frozen=True)
class ProviderAdapter:
    name: str
    call: Callable[[str, int], dict[str, Any]]


def default_adapters() -> tuple[ProviderAdapter, ...]:
    return (
        ProviderAdapter("gemini", _gemini_call),
        ProviderAdapter("groq", _groq_call),
        ProviderAdapter("openrouter", _openrouter_call),
    )


class ProviderRouter:
    """One pass over a bounded provider list; no nested or same-provider retry loop."""

    def __init__(self, adapters: Iterable[ProviderAdapter] | None = None) -> None:
        self.adapters = tuple(adapters or default_adapters())
        if not self.adapters:
            raise ValueError("at least one provider adapter is required")
        self.events: list[dict[str, Any]] = []

    def _event(
        self,
        *,
        stage: str,
        provider: str,
        result: str,
        wire_attempted: bool,
        reason: str | None,
        provider_attempt: int | None,
        stage_wire_attempt: int | None,
    ) -> None:
        self.events.append(
            {
                "timestamp": _utc_now(),
                "stage": stage,
                "provider": provider,
                "result": result,
                "wire_attempted": wire_attempted,
                "provider_attempt": provider_attempt,
                "stage_wire_attempt": stage_wire_attempt,
                "reason": reason,
            }
        )

    def route(
        self,
        *,
        stage: str,
        prompt: str,
        max_tokens: int,
        validator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > MAX_PROMPT_BYTES:
            self._event(
                stage=stage,
                provider="local",
                result="blocked",
                wire_attempted=False,
                reason="prompt_too_large",
                provider_attempt=None,
                stage_wire_attempt=None,
            )
            raise NoWireFailure("prompt_too_large")

        wire_count = 0
        failures: list[str] = []
        for adapter in self.adapters:
            try:
                candidate = adapter.call(prompt, max_tokens)
            except NoWireFailure as exc:
                failures.append(f"{adapter.name}:{exc.reason_code}")
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="unavailable",
                    wire_attempted=False,
                    reason=exc.reason_code,
                    provider_attempt=None,
                    stage_wire_attempt=None,
                )
                continue
            except Exception as exc:
                wire_count += 1
                reason = str(getattr(exc, "reason_code", "provider_failure"))
                failures.append(f"{adapter.name}:{reason}")
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="failed",
                    wire_attempted=True,
                    reason=reason,
                    provider_attempt=1,
                    stage_wire_attempt=wire_count,
                )
                continue

            wire_count += 1
            try:
                normalized = validator(candidate)
            except Exception as exc:
                reason = f"invalid_output_{type(exc).__name__.lower()}"
                failures.append(f"{adapter.name}:{reason}")
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="invalid_output",
                    wire_attempted=True,
                    reason=reason,
                    provider_attempt=1,
                    stage_wire_attempt=wire_count,
                )
                continue
            self._event(
                stage=stage,
                provider=adapter.name,
                result="success",
                wire_attempted=True,
                reason=None,
                provider_attempt=1,
                stage_wire_attempt=wire_count,
            )
            return normalized

        summary = ", ".join(failures) or "no providers configured"
        raise RuntimeError(f"{stage} exhausted bounded provider route: {summary}")
