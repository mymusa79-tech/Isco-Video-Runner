from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from . import mistral_executor


MAX_PROMPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_SHORT_RETRY_AFTER_SECONDS = 10.0
SHORT_RETRY_AFTER_STAGES = frozenset({"planning", "script", "script_patch"})
_MISTRAL_SHORT_HOOK_PROMPT_SUFFIX = """
MISTRAL_SHORT_HOOK_COMPLIANCE — mandatory preflight before returning JSON:
- The first spoken sentence (Hook) must be one complete natural Arabic sentence, preferably 8-16 words and NEVER more than 18.
- Count words exactly like the validator: split the first sentence on whitespace; each non-empty item is one word, even when punctuation is attached.
- Preserve grammar, approved factual meaning, and the information gap; do not shorten by deleting context needed for comprehension.
- Hook preflight: isolate s1 first sentence -> split on spaces -> count -> if count > 18, rewrite it more densely without fragmenting the sentence -> count again.

MISTRAL_SHORT_S3_COMPLIANCE — mandatory preflight before returning JSON:
- Isolate s3 and split it into complete sentences.
- Exactly ONE s3 sentence may contain a practical-action/imperative marker. That sentence must begin with a direct Arabic imperative verb and contain exactly ONE imperative/action marker.
- Every other s3 sentence is payoff/explanation only: ZERO command verbs and ZERO occurrences or derivatives of the forbidden action families already listed in SHORT_FORMAT_CONTRACT.
- Never join a second action with ثم, و, punctuation, or another clause inside the action sentence.
- Preflight algorithm: count action sentences -> require exactly 1 -> count imperative/action markers inside that sentence -> require exactly 1 -> scan every payoff sentence for forbidden action-family terms -> require zero. If any count fails, rewrite s3 completely and repeat the checks before returning JSON.
- Do not rely on downstream repair or trimming to fix Hook or s3.
""".strip()


def _provider_prompt(prompt: str, *, provider: str, stage: str) -> str:
    """Add narrow provider-specific guidance without changing other provider prompts."""
    if (
        provider == "mistral"
        and stage == "script"
        and "SHORT_FORMAT_CONTRACT:" in prompt
    ):
        return prompt.rstrip() + "\n\n" + _MISTRAL_SHORT_HOOK_PROMPT_SUFFIX
    return prompt

MISTRAL_NARRATIVE_IDENTITY_SCHEMA = {
    "type": "object",
    "properties": {
        "opener": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "closer": {"type": "string", "minLength": 1, "pattern": r"\S"},
        "transitions": {
            "type": "array",
            "prefixItems": [
                {"type": "string", "minLength": 1, "pattern": r"\S"},
                {"type": "string", "minLength": 1, "pattern": r"\S"},
                {"type": "string", "minLength": 1, "pattern": r"\S"},
            ],
            "minItems": 3,
            "maxItems": 3,
        },
    },
    "required": ["opener", "closer", "transitions"],
    "additionalProperties": False,
}

MISTRAL_VISUAL_QUERY_RECOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "alternate_query": {
            "type": "string",
            "maxLength": 80,
        }
    },
    "required": ["alternate_query"],
    "additionalProperties": False,
}


MISTRAL_SCRIPT_PATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "patches": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "section_id": {"type": "string", "minLength": 1, "maxLength": 40},
                    "find": {"type": "string", "minLength": 1, "maxLength": 400},
                    "replace": {"type": "string", "maxLength": 550},
                },
                "required": ["section_id", "find", "replace"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["patches"],
    "additionalProperties": False,
}


class NoWireFailure(RuntimeError):
    """A local rejection that is proven to happen before an HTTP request."""

    wire_attempted = False

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code or "local_rejection")
        super().__init__(self.reason_code)


class ProviderWireFailure(RuntimeError):
    wire_attempted = True

    def __init__(
        self,
        reason_code: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
        safe_detail: str | None = None,
    ) -> None:
        self.reason_code = str(reason_code or "provider_failure")
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.safe_detail = str(safe_detail or "").strip() or None
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


def _retry_after_seconds(headers: object) -> float | None:
    if headers is None:
        return None
    try:
        raw_value = headers.get("Retry-After")  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return None
    try:
        seconds = float(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _safe_http_error_detail(raw: bytes) -> str | None:
    """Extract only bounded provider error metadata; never echo request data."""
    if not raw:
        return None
    try:
        decoded = raw.decode("utf-8", errors="replace")
        payload = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None

    fields: list[str] = []
    for key in ("type", "code", "param", "message"):
        value = error.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        text = re.sub(r"[\r\n\t]+", " ", str(value)).strip()
        if not text:
            continue
        text = re.sub(r"\s{2,}", " ", text)
        if len(text) > 360:
            text = text[:357] + "..."
        fields.append(f"{key}={text}")
    if not fields:
        return None
    return " | ".join(fields)[:900]


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
        http_status = int(exc.code)
        try:
            error_raw = exc.read(8192)
        except Exception:
            error_raw = b""
        raise ProviderWireFailure(
            f"http_{http_status}",
            http_status=http_status,
            retry_after_seconds=(
                _retry_after_seconds(exc.headers) if http_status == 429 else None
            ),
            safe_detail=_safe_http_error_detail(error_raw),
        ) from None
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


def _gemini_call(prompt: str, max_tokens: int, *, response_schema: dict[str, Any] | None = None) -> dict[str, Any]:
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
                **({"responseJsonSchema": response_schema} if response_schema is not None else {}),
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


def _groq_call(prompt: str, max_tokens: int, *, response_schema: dict[str, Any] | None = None, schema_name: str = "isco_response") -> dict[str, Any]:
    key = _read_secret("GROQ_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("GROQ_CONTENT_MODEL") or "openai/gpt-oss-20b").strip()
    if not model:
        raise NoWireFailure("missing_model")
    try:
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
                "response_format": ({"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": response_schema}} if response_schema is not None else {"type": "json_object"}),
                "include_reasoning": False,
                "temperature": 0.3,
                "max_completion_tokens": int(max_tokens),
            },
            timeout=90,
        )
    except ProviderWireFailure as exc:
        if exc.http_status == 400 and exc.safe_detail:
            print(f"Groq HTTP 400 diagnostic: {exc.safe_detail}")
        raise
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderWireFailure("groq_no_choice")
    finish = str(choices[0].get("finish_reason") or "").strip().lower()
    if finish in {"length", "max_tokens"}:
        raise ProviderWireFailure("groq_output_truncated")
    message = choices[0].get("message") or {}
    return _parse_json_object(str(message.get("content") or ""), "groq")


def _openrouter_call(prompt: str, max_tokens: int, *, response_schema: dict[str, Any] | None = None, schema_name: str = "isco_response") -> dict[str, Any]:
    key = _read_secret("OPENROUTER_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("OPENROUTER_CONTENT_MODEL") or "google/gemma-4-26b-a4b-it:free").strip()
    if model != "google/gemma-4-26b-a4b-it:free":
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
            "response_format": ({"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": response_schema}} if response_schema is not None else {"type": "json_object"}),
            "provider": {"allow_fallbacks": True, **({"require_parameters": True} if response_schema is not None else {})},
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


def _safe_mistral_script_raw_diagnostic(raw_content: str, exc: Exception) -> dict[str, Any]:
    """Describe rejected Mistral Script output without logging narration text."""
    raw = str(raw_content or "")
    raw_bytes = raw.encode("utf-8")
    diagnostic: dict[str, Any] = {
        "validator_error_type": type(exc).__name__,
        "validator_error": str(exc)[:500],
        "raw_content": {
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "utf8_bytes": len(raw_bytes),
        },
    }
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        diagnostic["raw_content"]["shape"] = {"json_type": "invalid_json"}
        return diagnostic

    if not isinstance(value, dict):
        diagnostic["raw_content"]["shape"] = {
            "json_type": type(value).__name__,
        }
        return diagnostic

    shape: dict[str, Any] = {
        "json_type": "object",
        "top_level_keys": sorted(str(key)[:80] for key in value.keys())[:30],
    }
    title = value.get("title")
    shape["title_type"] = type(title).__name__
    shape["title_chars"] = len(title) if isinstance(title, str) else None

    sections = value.get("sections")
    shape["sections_type"] = type(sections).__name__
    if isinstance(sections, list):
        shape["sections_count"] = len(sections)
        section_shapes: list[dict[str, Any]] = []
        for index, item in enumerate(sections[:10]):
            if not isinstance(item, dict):
                section_shapes.append(
                    {"index": index, "json_type": type(item).__name__}
                )
                continue
            narration = item.get("narration")
            section_shapes.append(
                {
                    "index": index,
                    "keys": sorted(str(key)[:80] for key in item.keys())[:20],
                    "id": str(item.get("id") or "")[:40],
                    "narration_type": type(narration).__name__,
                    "narration_chars": (
                        len(narration) if isinstance(narration, str) else None
                    ),
                }
            )
        shape["sections"] = section_shapes
    diagnostic["raw_content"]["shape"] = shape
    return diagnostic


def _safe_validator_reason(exc: Exception) -> str:
    """Persist only a deterministic validator code, never rejected content."""
    base = f"invalid_output_{type(exc).__name__.lower()}"
    if type(exc).__name__ != "ShortFormatError":
        return base
    code = str(exc).strip().split(maxsplit=1)[0].casefold()
    if re.fullmatch(r"[a-z0-9_]{1,120}", code):
        return f"{base}_{code}"
    return base


def _safe_mistral_script_patch_raw_diagnostic(
    raw_content: str, exc: Exception
) -> dict[str, Any]:
    """Describe rejected Mistral script_patch output without logging find/replace text."""
    raw = str(raw_content or "")
    raw_bytes = raw.encode("utf-8")
    diagnostic: dict[str, Any] = {
        "validator_error_type": type(exc).__name__,
        "validator_error": str(exc)[:500],
        "raw_content": {
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "utf8_bytes": len(raw_bytes),
        },
    }
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        diagnostic["raw_content"]["shape"] = {"json_type": "invalid_json"}
        return diagnostic

    if not isinstance(value, dict):
        diagnostic["raw_content"]["shape"] = {"json_type": type(value).__name__}
        return diagnostic

    shape: dict[str, Any] = {
        "json_type": "object",
        "top_level_keys": sorted(str(key)[:80] for key in value.keys())[:30],
    }
    patches = value.get("patches")
    shape["patches_type"] = type(patches).__name__
    if isinstance(patches, list):
        shape["patches_count"] = len(patches)
        patch_shapes: list[dict[str, Any]] = []
        for index, item in enumerate(patches[:10]):
            if not isinstance(item, dict):
                patch_shapes.append({"index": index, "json_type": type(item).__name__})
                continue
            find = item.get("find")
            replace = item.get("replace")
            patch_shapes.append(
                {
                    "index": index,
                    "keys": sorted(str(key)[:80] for key in item.keys())[:20],
                    "section_id": str(item.get("section_id") or "")[:40],
                    "find_type": type(find).__name__,
                    "find_chars": len(find) if isinstance(find, str) else None,
                    "replace_type": type(replace).__name__,
                    "replace_chars": len(replace) if isinstance(replace, str) else None,
                }
            )
        shape["patches"] = patch_shapes
    diagnostic["raw_content"]["shape"] = shape
    return diagnostic


def _mistral_planning_response_schema(prompt: str) -> dict[str, Any]:
    """Build Planning schema from validate_plan() semantics and the approved format."""
    marker = "APPROVED_BRIEF:\n"
    terminator = "\n\nBuild a simple production plan."
    if marker not in prompt:
        raise NoWireFailure("mistral_planning_approved_brief_missing")
    brief_tail = prompt.split(marker, 1)[1]
    if terminator not in brief_tail:
        raise NoWireFailure("mistral_planning_approved_brief_boundary_missing")
    raw_brief = brief_tail.split(terminator, 1)[0].strip()
    try:
        brief = json.loads(raw_brief)
    except json.JSONDecodeError:
        raise NoWireFailure("mistral_planning_approved_brief_invalid_json") from None
    if not isinstance(brief, dict):
        raise NoWireFailure("mistral_planning_approved_brief_invalid_shape")

    fmt = str(brief.get("format") or "").strip().lower()
    if fmt == "film":
        min_sections = max_sections = 5
    elif fmt == "short":
        min_sections = max_sections = 3
    elif fmt in {"moment", "story"}:
        min_sections, max_sections = 1, 5
    else:
        raise NoWireFailure("mistral_planning_unsupported_format")

    non_blank_string = {"type": "string", "minLength": 1, "pattern": r"\S"}
    if fmt == "short":
        cta_schema = {"type": "string", "const": ""}
    elif fmt == "moment":
        cta_schema = {"type": "string"}
    else:
        cta_schema = dict(non_blank_string)
    section_properties = {
        # validate_plan() deliberately synthesizes sN when id is omitted.
        "id": dict(non_blank_string),
        "heading": dict(non_blank_string),
        "purpose": dict(non_blank_string),
        "visual_query_en": dict(non_blank_string),
    }
    section_required = ["heading", "purpose", "visual_query_en"]
    if fmt == "short":
        section_properties["visual_query_alt_en"] = dict(non_blank_string)
        section_required.append("visual_query_alt_en")
    section_schema = {
        "type": "object",
        "properties": section_properties,
        "required": section_required,
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": dict(non_blank_string),
            "promise": dict(non_blank_string),
            "cta": cta_schema,
            "sections": {
                "type": "array",
                "items": section_schema,
                "minItems": min_sections,
                "maxItems": max_sections,
            },
        },
        "required": ["title", "promise", "cta", "sections"],
        "additionalProperties": False,
    }


def _mistral_script_response_schema(prompt: str) -> dict[str, Any]:
    """Build a strict Script schema from the internally generated LOCKED_PLAN."""
    marker = "LOCKED_PLAN:\n"
    terminator = "\n\nThe approved brief and locked plan are authoritative."
    if marker not in prompt:
        raise NoWireFailure("mistral_script_locked_plan_missing")
    locked_tail = prompt.split(marker, 1)[1]
    if terminator not in locked_tail:
        raise NoWireFailure("mistral_script_locked_plan_boundary_missing")
    raw_plan = locked_tail.split(terminator, 1)[0].strip()
    try:
        plan = json.loads(raw_plan)
    except json.JSONDecodeError:
        raise NoWireFailure("mistral_script_locked_plan_invalid_json") from None
    if not isinstance(plan, dict) or not isinstance(plan.get("sections"), list):
        raise NoWireFailure("mistral_script_locked_plan_invalid_shape")

    section_ids: list[str] = []
    for raw_section in plan["sections"]:
        if not isinstance(raw_section, dict):
            raise NoWireFailure("mistral_script_locked_plan_invalid_section")
        section_id = str(raw_section.get("id") or "").strip()
        if not section_id or len(section_id) > 40:
            raise NoWireFailure("mistral_script_locked_plan_invalid_id")
        section_ids.append(section_id)

    if not 1 <= len(section_ids) <= 5 or len(section_ids) != len(set(section_ids)):
        raise NoWireFailure("mistral_script_locked_plan_invalid_ids")

    section_schemas = [
        {
            "type": "object",
            "properties": {
                "id": {"type": "string", "const": section_id},
                "narration": {"type": "string", "minLength": 20},
            },
            "required": ["id", "narration"],
            "additionalProperties": False,
        }
        for section_id in section_ids
    ]
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "sections": {
                "type": "array",
                "prefixItems": section_schemas,
                "minItems": len(section_schemas),
                "maxItems": len(section_schemas),
            },
        },
        "required": ["title", "sections"],
        "additionalProperties": False,
    }


def _groq_planning_response_schema(prompt: str) -> dict[str, Any]:
    """Strict Groq schema for plan shape; semantic checks remain local."""
    source = _mistral_planning_response_schema(prompt)
    source_section = source["properties"]["sections"]["items"]
    section_properties = {
        key: {"type": "string"}
        for key in source_section["properties"]
    }
    section_schema = {
        "type": "object",
        "properties": section_properties,
        "required": list(section_properties),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "promise": {"type": "string"},
            "cta": {"type": "string"},
            "sections": {
                "type": "array",
                "items": section_schema,
                "minItems": int(source["properties"]["sections"]["minItems"]),
                "maxItems": int(source["properties"]["sections"]["maxItems"]),
            },
        },
        "required": ["title", "promise", "cta", "sections"],
        "additionalProperties": False,
    }


def _groq_script_response_schema(prompt: str) -> dict[str, Any]:
    """Strict Groq schema for script shape; exact order/semantics stay local."""
    source = _mistral_script_response_schema(prompt)
    section_ids = [
        str(item["properties"]["id"]["const"])
        for item in source["properties"]["sections"]["prefixItems"]
    ]
    section_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": section_ids},
            "narration": {"type": "string"},
        },
        "required": ["id", "narration"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "sections": {
                "type": "array",
                "items": section_schema,
                "minItems": len(section_ids),
                "maxItems": len(section_ids),
            },
        },
        "required": ["title", "sections"],
        "additionalProperties": False,
    }


def _groq_stage_call(prompt: str, max_tokens: int, stage: str) -> dict[str, Any]:
    if stage == "planning":
        return _groq_call(
            prompt,
            max_tokens,
            response_schema=_groq_planning_response_schema(prompt),
            schema_name="planning",
        )
    if stage == "script":
        return _groq_call(
            prompt,
            max_tokens,
            response_schema=_groq_script_response_schema(prompt),
            schema_name="script",
        )
    return _groq_call(prompt, max_tokens)


def _mistral_call(prompt: str, max_tokens: int, stage: str) -> dict[str, Any]:
    try:
        if stage == "visual_query_recovery":
            return mistral_executor.mistral_executor_json(
                prompt,
                max_tokens=max_tokens,
                task_kind=stage,
                response_schema=(
                    "visual_query_recovery",
                    MISTRAL_VISUAL_QUERY_RECOVERY_SCHEMA,
                ),
            )
        if stage == "planning":
            return mistral_executor.mistral_executor_json(
                prompt,
                max_tokens=max_tokens,
                task_kind=stage,
                response_schema=(
                    "planning",
                    _mistral_planning_response_schema(prompt),
                ),
                temperature=0.0,
            )
        if stage == "script":
            return mistral_executor.mistral_executor_json(
                prompt,
                max_tokens=max_tokens,
                task_kind=stage,
                response_schema=(
                    "script",
                    _mistral_script_response_schema(prompt),
                ),
            )
        if stage == "script_patch":
            return mistral_executor.mistral_executor_json(
                prompt,
                max_tokens=max_tokens,
                task_kind=stage,
                response_schema=(
                    "script_patch",
                    MISTRAL_SCRIPT_PATCH_SCHEMA,
                ),
                temperature=0.0,
            )
        if stage == "narrative_identity":
            return mistral_executor.mistral_executor_json(
                prompt,
                max_tokens=max_tokens,
                task_kind=stage,
                response_schema=(
                    "narrative_identity",
                    MISTRAL_NARRATIVE_IDENTITY_SCHEMA,
                ),
            )
        return mistral_executor.mistral_executor_json(
            prompt,
            max_tokens=max_tokens,
            task_kind=stage,
        )
    except mistral_executor.MistralExecutorNoWireFailure as exc:
        raise NoWireFailure(exc.reason_code) from None
    except mistral_executor.MistralExecutorWireFailure as exc:
        raise ProviderWireFailure(exc.reason_code) from None


@dataclass(frozen=True)
class ProviderAdapter:
    name: str
    call: Callable[..., dict[str, Any]]
    stages: frozenset[str] | None = None
    accepts_stage: bool = False

    def invoke(self, prompt: str, max_tokens: int, stage: str) -> dict[str, Any]:
        if self.accepts_stage:
            return self.call(prompt, max_tokens, stage)
        return self.call(prompt, max_tokens)


def default_adapters() -> tuple[ProviderAdapter, ...]:
    return (
        ProviderAdapter("gemini", _gemini_call),
        ProviderAdapter("groq", _groq_stage_call, accepts_stage=True),
        ProviderAdapter("openrouter", _openrouter_call),
        ProviderAdapter(
            "mistral",
            _mistral_call,
            stages=frozenset({"planning", "narrative_identity", "script", "script_patch", "visual_query_recovery"}),
            accepts_stage=True,
        ),
    )


class ProviderRouter:
    """One pass over a bounded provider list.

    The only same-provider exception is one Mistral Planning re-issue when the
    provider returns syntactically invalid JSON despite strict json_schema mode.
    This is a bounded provider-contract retry, not a second provider sweep.
    """

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
        eligible_adapters = tuple(
            adapter
            for adapter in self.adapters
            if adapter.stages is None or stage in adapter.stages
        )
        for adapter_index, adapter in enumerate(eligible_adapters):
            candidate: dict[str, Any] | None = None
            provider_attempt = 0
            provider_failed = False
            while True:
                provider_attempt += 1
                try:
                    provider_prompt = _provider_prompt(
                        prompt,
                        provider=adapter.name,
                        stage=stage,
                    )
                    candidate = adapter.invoke(provider_prompt, max_tokens, stage)
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
                    provider_failed = True
                    break
                except Exception as exc:
                    wire_count += 1
                    reason = str(getattr(exc, "reason_code", "provider_failure"))
                    strict_schema_retry = (
                        adapter.name == "mistral"
                        and stage == "planning"
                        and reason == "mistral invalid json"
                        and provider_attempt == 1
                    )
                    if strict_schema_retry:
                        self._event(
                            stage=stage,
                            provider=adapter.name,
                            result="retrying",
                            wire_attempted=True,
                            reason="mistral_strict_schema_invalid_json",
                            provider_attempt=provider_attempt,
                            stage_wire_attempt=wire_count,
                        )
                        continue

                    failures.append(f"{adapter.name}:{reason}")
                    self._event(
                        stage=stage,
                        provider=adapter.name,
                        result="failed",
                        wire_attempted=True,
                        reason=reason,
                        provider_attempt=provider_attempt,
                        stage_wire_attempt=wire_count,
                    )
                    retry_after = getattr(exc, "retry_after_seconds", None)
                    if (
                        stage in SHORT_RETRY_AFTER_STAGES
                        and getattr(exc, "http_status", None) == 429
                        and adapter_index + 1 < len(eligible_adapters)
                        and isinstance(retry_after, (int, float))
                        and 0 < float(retry_after) <= MAX_SHORT_RETRY_AFTER_SECONDS
                    ):
                        time.sleep(float(retry_after))
                    provider_failed = True
                    break
                else:
                    wire_count += 1
                    break

            if provider_failed or candidate is None:
                continue

            try:
                normalized = validator(candidate)
            except Exception as exc:
                if adapter.name == "mistral" and stage == "visual_query_recovery":
                    raw_content = mistral_executor.get_last_mistral_executor_raw_content()
                    print(
                        "Mistral visual_query_recovery validator rejected raw content: "
                        + json.dumps(
                            {"raw_content": raw_content},
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                elif adapter.name == "mistral" and stage == "script":
                    raw_content = mistral_executor.get_last_mistral_executor_raw_content()
                    print(
                        "Mistral script validator rejected raw content: "
                        + json.dumps(
                            _safe_mistral_script_raw_diagnostic(raw_content, exc),
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                elif adapter.name == "mistral" and stage == "script_patch":
                    raw_content = mistral_executor.get_last_mistral_executor_raw_content()
                    print(
                        "Mistral script_patch validator rejected raw content: "
                        + json.dumps(
                            _safe_mistral_script_patch_raw_diagnostic(raw_content, exc),
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                reason = _safe_validator_reason(exc)
                failures.append(f"{adapter.name}:{reason}")
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="invalid_output",
                    wire_attempted=True,
                    reason=reason,
                    provider_attempt=provider_attempt,
                    stage_wire_attempt=wire_count,
                )
                continue
            self._event(
                stage=stage,
                provider=adapter.name,
                result="success",
                wire_attempted=True,
                reason=None,
                provider_attempt=provider_attempt,
                stage_wire_attempt=wire_count,
            )
            return normalized

        summary = ", ".join(failures) or "no providers configured"
        raise RuntimeError(f"{stage} exhausted bounded provider route: {summary}")
