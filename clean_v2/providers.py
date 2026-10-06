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
from typing import Any, Callable, Iterable, Mapping

from . import mistral_executor


MAX_PROMPT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
# Bounded snippet kept from a provider's HTTP error body for diagnostics only
# (provider-events.json / error_detail) - never the full body, and never logged
# anywhere that would echo the outbound prompt or an API key back.
MAX_ERROR_DETAIL_BYTES = 2 * 1024
# Groq's free-tier chat/completions endpoint rejects large single-shot JSON
# prompts with HTTP 413 well below the Runner's own MAX_PROMPT_BYTES ceiling
# (observed around ~41 KB on Run 66). Even a minimal Planning/Script prompt
# with an empty brief already carries ~26-33 KB of fixed instruction text
# before any real topic/research content is added, so the local pre-check
# must sit close to (but still safely under) the observed 413 threshold
# rather than near the fixed-overhead floor - too tight and Groq is skipped
# on every ordinary prompt, defeating the whole point of keeping it in the
# cascade. 38 KiB leaves ~3 KB of headroom below the known-bad size while
# still passing typical Planning/Script prompts through unaffected.
GROQ_MAX_PROMPT_UTF8_BYTES = 38 * 1024
# Planning has extra prompt/schema overhead. Runs 80/81 showed that 33-36 KiB
# planning prompts can exceed Groq's 8k TPM request budget after tokenization.
# Keep a real safety margin for Planning; other Groq stages retain 38 KiB.
GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES = 30 * 1024
MAX_SHORT_RETRY_AFTER_SECONDS = 10.0
SHORT_RETRY_AFTER_STAGES = frozenset({"planning", "script", "script_patch"})
# Mistral is often the last Planning provider standing and each rejection names a
# different local-contract rule (run 83/84/94: identity -> coverage -> semantic drop).
# One correction was not enough; two keep every validator intact while giving the
# repair loop room to converge. Other stages keep their single correction.
MISTRAL_PLANNING_MAX_VALIDATOR_RETRIES = 2

# A 429 whose own body says the limit is temporary (OpenRouter free models: "temporarily
# rate-limited upstream ... retry shortly") must not poison the provider for the rest of
# the run, and through the text-audit circuit seed it must not remove a judge from the
# audit panel. Real quota exhaustion still circuits: the quota markers always win.
_TRANSIENT_429_MARKERS = (
    "temporarily rate-limited",
    "temporarily rate limited",
    "retry shortly",
    "rate-limited upstream",
)
_QUOTA_429_MARKERS = (
    "exceeded your current quota",
    "quota exceeded",
    "daily quota",
    "per day",
    "insufficient quota",
    "spend limit",
)


def _is_transient_429_detail(detail: object) -> bool:
    lowered = str(detail or "").lower()
    if not lowered or any(marker in lowered for marker in _QUOTA_429_MARKERS):
        return False
    return any(marker in lowered for marker in _TRANSIENT_429_MARKERS)
# Mirrors CHARON_RETRY_DELAYS_SECONDS[0] in media.py: a single short same-provider
# retry for a classic transient server/network failure only (502/503/504 or a
# transport-level error), never for a genuine client error or the 429/quota path
# already handled separately above. One extra attempt (two total) is enough to
# recover a real blip without spending meaningful time against a sustained outage.
TRANSIENT_RETRY_DELAY_SECONDS = 1.0
_TRANSIENT_HTTP_STATUSES = frozenset({502, 503, 504})
_MISTRAL_SHORT_HOOK_PROMPT_SUFFIX = """
MISTRAL_SHORT_HOOK_COMPLIANCE — mandatory preflight before returning JSON:
- The first spoken sentence (Hook) must be one complete natural Arabic sentence, TARGET 12-16 words and NEVER more than 18.
- Count words exactly like the validator: split the first sentence on whitespace; each non-empty item is one word, even when punctuation is attached.
- Preserve grammar, approved factual meaning, and the information gap; do not shorten by deleting context needed for comprehension.
- Hook preflight: isolate s1 first sentence -> split on spaces -> count -> if count > 16, rewrite it more densely as a complete 12-16 word sentence without fragmenting it -> count again.
- Treat 17-18 words as validator headroom only, not a writing target. Never intentionally return a 17-18 word first draft when the same meaning can be expressed naturally in 12-16 words.
- If a complete draft still lands at 17-20 words, DO NOT return it. Rewrite the first sentence itself to 12-16 words and move secondary detail to sentence two, then recount the first sentence.

MISTRAL_SHORT_S3_COMPLIANCE — mandatory preflight before returning JSON:
- LOCKED_PLAN.practical_action_ar is host-owned and will be appended by runtime after validation. Do NOT write, repeat, paraphrase, or replace it.
- Isolate s3 and write at least one complete descriptive payoff/explanation sentence that resolves the same hook tension.
- Return that authored Short closing text in the s3_payoff field. Do not return a narration field for s3 and never return s3_locked_action; runtime injects the Planning-owned action.
- Every authored s3 sentence must contain ZERO practical-action/imperative markers and ZERO occurrences or derivatives of the forbidden action families already listed in SHORT_FORMAT_CONTRACT.
- If any s3 sentence contains advice or an action-family term, rewrite that sentence as a purely descriptive state/result and rescan s3 from the beginning.
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
        error_detail: str | None = None,
    ) -> None:
        self.reason_code = str(reason_code or "provider_failure")
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        # Bounded, best-effort snippet of the provider's own error body (never the
        # request payload, prompt, or API key) so a genuine client error (400/413)
        # can be diagnosed from provider-events.json instead of guessed at from the
        # status code alone.
        self.error_detail = error_detail
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


def _is_transient_wire_failure(exc: BaseException) -> bool:
    """A brief same-provider retry is safe only for a classic transient
    server/network failure - never a genuine client error, and never the
    429/quota case, which already has its own dedicated handling."""
    if getattr(exc, "http_status", None) in _TRANSIENT_HTTP_STATUSES:
        return True
    reason_code = str(getattr(exc, "reason_code", "") or "")
    return reason_code.startswith("transport_")


def _safe_error_detail(raw: bytes) -> str | None:
    """Decode a provider's HTTP error body into a short, loggable diagnostic.

    Bounded to MAX_ERROR_DETAIL_BYTES and never includes the request we sent
    (prompt, schema, or API key) - only what the provider sent back, which is
    what tells us whether a 400/413 is a schema problem, a size problem, or
    something else entirely.
    """
    body = bytes(raw or b"")[:MAX_ERROR_DETAIL_BYTES]
    if not body:
        return None
    try:
        text_value = body.decode("utf-8", errors="replace")
    except Exception:
        return None
    text_value = " ".join(text_value.split()).strip()
    return text_value[:MAX_ERROR_DETAIL_BYTES] or None


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
            error_body = exc.read(MAX_ERROR_DETAIL_BYTES + 1)
        except Exception:
            error_body = b""
        error_detail = _safe_error_detail(error_body)
        raise ProviderWireFailure(
            f"http_{http_status}",
            http_status=http_status,
            retry_after_seconds=(
                _retry_after_seconds(exc.headers) if http_status == 429 else None
            ),
            error_detail=error_detail,
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
    model = str(os.environ.get("GEMINI_CONTENT_MODEL") or "gemini-3.7-flash").strip()
    return _gemini_call_with_model(prompt, max_tokens, model=model, response_schema=response_schema)


def _gemini_flash_lite_call(prompt: str, max_tokens: int, *, response_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    # gemini-3.1-flash-lite shares GEMINI_API_KEY's free tier with the primary
    # gemini-3.7-flash model but carries a far higher daily request quota
    # (~500/day vs. ~20/day), so it sits between "gemini" and "groq" as a
    # same-family fallback that only fires once the primary model is exhausted.
    model = str(os.environ.get("GEMINI_FLASH_LITE_CONTENT_MODEL") or "gemini-3.1-flash-lite").strip()
    return _gemini_call_with_model(prompt, max_tokens, model=model, response_schema=response_schema)


def _gemini_call_with_model(
    prompt: str,
    max_tokens: int,
    *,
    model: str,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    key = _read_secret("GEMINI_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
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
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ProviderWireFailure("groq_no_choice")
    finish = str(choices[0].get("finish_reason") or "").strip().lower()
    if finish in {"length", "max_tokens"}:
        raise ProviderWireFailure("groq_output_truncated")
    message = choices[0].get("message") or {}
    return _parse_json_object(str(message.get("content") or ""), "groq")


def _openrouter_404_with_diagnostic(exc: "ProviderWireFailure") -> "ProviderWireFailure":
    """Keep reason_code 'http_404' but expose OpenRouter's own 404 body in str(exc).

    Audit attempt details are built from str(exc) and otherwise only say "http_404",
    which cannot tell "no endpoints found" from a policy/parameter 404. Words the
    Engine's error classifier keys on are neutralised so this never changes how the
    failure is classified.
    """
    snippet = " ".join(str(exc.error_detail or "").split())[:160]
    for word in ("429", "quota", "rate limit", "timeout", "timed out", "connection", "network", "premature", "invalid json"):
        snippet = snippet.replace(word, word[0] + "_" + word[1:].replace(" ", "_"))
    wrapped = ProviderWireFailure(
        exc.reason_code, http_status=exc.http_status, error_detail=exc.error_detail
    )
    wrapped.args = (f"{exc.reason_code} openrouter_body={snippet}",)
    return wrapped


def _openrouter_call(prompt: str, max_tokens: int, *, response_schema: dict[str, Any] | None = None, schema_name: str = "isco_response") -> dict[str, Any]:
    key = _read_secret("OPENROUTER_API_KEY")
    if not key:
        raise NoWireFailure("missing_api_key")
    model = str(os.environ.get("OPENROUTER_CONTENT_MODEL") or "google/gemma-4-26b-a4b-it:free").strip()
    if model != "google/gemma-4-26b-a4b-it:free":
        raise NoWireFailure("paid_or_unapproved_model")
    try:
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
    except ProviderWireFailure as exc:
        if exc.http_status == 404 and exc.error_detail:
            raise _openrouter_404_with_diagnostic(exc) from None
        raise
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


def _safe_mistral_planning_raw_diagnostic(
    raw_content: str, exc: Exception
) -> dict[str, Any]:
    """Describe rejected Mistral Planning output without logging authored text."""
    raw = str(raw_content or "")
    raw_bytes = raw.encode("utf-8")
    diagnostic: dict[str, Any] = {
        "validator_error_type": type(exc).__name__,
        "validator_error": " ".join(str(exc).split())[:500],
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
        "top_level_keys": sorted(str(key)[:80] for key in value.keys())[:40],
    }
    sections = value.get("sections")
    shape["sections_type"] = type(sections).__name__
    if isinstance(sections, list):
        shape["sections_count"] = len(sections)
        shape["section_shapes"] = [
            {
                "index": index,
                "json_type": type(item).__name__,
                **(
                    {"keys": sorted(str(key)[:80] for key in item.keys())[:20]}
                    if isinstance(item, dict)
                    else {}
                ),
            }
            for index, item in enumerate(sections[:10])
        ]

    story = value.get("visual_story")
    shape["visual_story_type"] = type(story).__name__
    if isinstance(story, dict):
        shape["visual_story_keys"] = sorted(
            str(key)[:80] for key in story.keys()
        )[:30]
        beats = story.get("beats")
        shape["beats_type"] = type(beats).__name__
        if isinstance(beats, list):
            shape["beats_count"] = len(beats)
            shape["beat_shapes"] = [
                {
                    "index": index,
                    "json_type": type(item).__name__,
                    **(
                        {"keys": sorted(str(key)[:80] for key in item.keys())[:24]}
                        if isinstance(item, dict)
                        else {}
                    ),
                }
                for index, item in enumerate(beats[:15])
            ]
    diagnostic["raw_content"]["shape"] = shape
    return diagnostic


def _mistral_planning_validator_retry_prompt(
    prompt: str,
    exc: Exception,
) -> str | None:
    """Give Mistral one bounded correction for a local Planning-contract rejection."""
    # VisualWorldIdentityError (missing the channel's required navy/gold visual
    # identity markers) is a deterministic, mechanically correctable rejection
    # just like the other three - Run 66 showed Mistral's only Planning attempt
    # being discarded outright on this error with zero correction chance, burning
    # the last adapter in the cascade for a fixable one-field omission.
    if type(exc).__name__ not in {
        "ValueError",
        "ContractError",
        "ShortFormatError",
        "VisualWorldIdentityError",
    }:
        return None
    detail = " ".join(str(exc).split()).strip()[:500]
    if not detail:
        return None
    correction = ""
    if type(exc).__name__ == "ShortFormatError" and detail.startswith("short_practical_action_"):
        correction = (
            "For practical_action_ar, write one imperative followed only by its topic-specific object/behavior, "
            "at most 18 words. Remove any second verb, ثم/و or attached conjunction (such as والتزم/واكتب), "
            "and any extra advice clause; preserve this topic's own action target. "
        )
    elif detail.startswith("visual_story must cover every planned section: missing="):
        missing = detail.split("missing=", 1)[1].strip()
        correction = (
            f"The visual_story omitted these planned section ids: {missing}. "
            "Add or repair beats so EVERY named missing section has at least one beat whose section_id exactly "
            "matches that section. Keep all existing valid section ids/order/count unchanged; do not solve this "
            "by deleting another section's beat. Reuse that section's own purpose/visual query as the semantic "
            "source and keep each beat concrete, observable, and stock-searchable. "
        )
    elif "post-hook semantic drop requires a stronger observable alternate" in detail:
        beat_match = re.search(r"visual_story beat\s+([A-Za-z0-9_-]+)", detail)
        beat_id = beat_match.group(1) if beat_match else "the rejected beat"
        correction = (
            f"For {beat_id}, the current post-hook visual is too generic. Keep the SAME section_id and meaning, "
            "but replace its shot_intent/stock_query_en or provide stock_query_alt_en with a stronger concrete "
            "observable action, consequence, contrast, or object-state that visibly proves the section idea. "
            "Do not return generic typing, scrolling, phone/laptop use, passive desk work, or mood-only footage. "
            "The corrected English query must be distinct from the rejected generic query and remain realistic "
            "stock footage. "
        )
    return (
        prompt.rstrip()
        + "\n\nMISTRAL_PLANNING_VALIDATOR_RETRY — the previous complete Planning JSON "
        + "was rejected by the local production validator. "
        + f"Exact rejection: {detail}. "
        + correction
        + "Return the COMPLETE Planning JSON again, correcting that exact rule only where needed. "
        + "Preserve the APPROVED_BRIEF, format, section ids/order/count, all quality and safety "
        + "contracts, and all required visual-story semantics. For Short, preserve the EXACTLY "
        + "5-beat house cut (three distinct s1 hook beats, then one s2 body beat, then one s3 "
        + "payoff beat) and keep the social CTA empty. Do not explain the correction. Return JSON only."
    )


def _mistral_script_patch_validator_retry_prompt(
    prompt: str,
    exc: Exception,
) -> str | None:
    """One same-provider correction when a bounded patch misses required sections.

    This remains part of the single repair pass: no second audit/repair cycle is
    opened. It only lets Mistral correct its rejected patch JSON once, exactly
    like Planning and Short-hook validator retries already do.
    """
    if type(exc).__name__ != "ValueError":
        return None
    detail = " ".join(str(exc).split()).strip()[:500]
    prefix = "semantic script patch did not change every explicitly flagged section:"
    if not detail.startswith(prefix):
        return None
    missing = detail.split(":", 1)[1].strip()
    if not missing:
        return None
    return (
        prompt.rstrip()
        + "\n\nMISTRAL_SCRIPT_PATCH_VALIDATOR_RETRY — your previous patch JSON was rejected "
        + "because it did not make a real narration change in every explicitly required semantic section. "
        + f"Missing required section ids: {missing}. "
        + "Return the COMPLETE patch JSON again. Include at least one valid minimal exact find/replace patch "
        + "for EACH missing section id, while also keeping the other listed defects fixed. Each patch.find "
        + "must be copied VERBATIM from CURRENT_SCRIPT in that exact section and must match exactly once. "
        + "Do not patch unflagged sections, do not touch locked prayer/channel/CTA/action text unless the "
        + "original contract explicitly allows it, and do not rewrite the whole script. Return JSON only."
    )


def _safe_validator_reason(exc: Exception) -> str:
    """Persist only a deterministic validator code, never rejected content."""
    base = f"invalid_output_{type(exc).__name__.lower()}"
    if type(exc).__name__ == "AlternateQueryError":
        # Preserve the historical ValueError prefix for existing route/log
        # consumers while exposing the precise rejection code after it.
        base = "invalid_output_valueerror"
        code = str(getattr(exc, "code", ""))
    elif type(exc).__name__ == "ShortFormatError":
        code = str(exc).strip().split(maxsplit=1)[0].casefold()
    else:
        return base
    if re.fullmatch(r"[a-z0-9_]{1,120}", code):
        return f"{base}_{code}"
    return base


def _mistral_short_hook_validator_retry_prompt(
    prompt: str,
    exc: Exception,
) -> str | None:
    """One bounded same-provider correction for Run 60's overlong Short hook.

    Never raises the hook limit or edits narration locally. Retry only when the
    validator says the Mistral Short hook is 21-32 words, so Mistral gets one
    chance to rewrite the first sentence to 12-16 words while preserving all
    section meaning and the locked s3 contract.
    """
    if type(exc).__name__ != "ShortFormatError":
        return None
    match = re.fullmatch(
        r"short_hook_too_long\s+words=(\d+)\s+maximum=(\d+)",
        str(exc).strip(),
    )
    if not match:
        return None
    words = int(match.group(1))
    if not 21 <= words <= 32:
        return None
    return (
        prompt.rstrip()
        + "\n\nMISTRAL_SHORT_HOOK_VALIDATOR_RETRY — previous output was rejected "
        + f"because its first spoken sentence had {words} words. "
        + "Return the COMPLETE script JSON again. Rewrite ONLY the first spoken "
        + "sentence as one natural, self-contained Arabic hook of 12-16 words "
        + "(hard maximum 18). Move secondary detail into sentence two; preserve "
        + "the same meaning, all section ids/order, and every other contract. "
        + "Count the rewritten first sentence by whitespace before returning JSON."
    )


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
    elif fmt == "podcast":
        min_sections, max_sections = 2, 5
    elif fmt == "short":
        min_sections = max_sections = 3
    elif fmt in {"moment", "story"}:
        min_sections, max_sections = 1, 5
    else:
        raise NoWireFailure("mistral_planning_unsupported_format")

    non_blank_string = {"type": "string", "minLength": 1, "pattern": r"\S"}
    long_form = fmt in {"film", "podcast"}

    def planning_string(max_length: int) -> dict[str, Any]:
        schema = dict(non_blank_string)
        if long_form:
            schema["maxLength"] = max_length
        return schema

    if fmt == "short":
        cta_schema = {"type": "string", "const": ""}
    elif fmt == "moment":
        cta_schema = {"type": "string"}
    else:
        cta_schema = planning_string(140)
    section_properties = {
        # validate_plan() deliberately synthesizes sN when id is omitted.
        "id": planning_string(40),
        "heading": planning_string(80),
        "purpose": planning_string(240),
        "visual_query_en": planning_string(140),
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
    visual_story_schema = {
        "type": "object",
        "properties": {
            "visual_world": dict(non_blank_string),
            "story_arc": {
                "type": "object",
                "properties": {
                    "beginning": dict(non_blank_string),
                    "transformation": dict(non_blank_string),
                    "arrival": dict(non_blank_string),
                },
                "required": ["beginning", "transformation", "arrival"],
                "additionalProperties": False,
            },
            "retention_thread": {
                "type": "object",
                "properties": {
                    "hook_tension": dict(non_blank_string),
                    "payoff_answer": dict(non_blank_string),
                    "visual_motif": dict(non_blank_string),
                },
                "required": [
                    "hook_tension",
                    "payoff_answer",
                    "visual_motif",
                ],
                "additionalProperties": False,
            },
            "beats": {
                "type": "array",
                "minItems": min_sections,
                "maxItems": max_sections * 3,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": dict(non_blank_string),
                        "section_id": dict(non_blank_string),
                        "viewer_intent": dict(non_blank_string),
                        "meaning_target": dict(non_blank_string),
                        "semantic_must_have": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": dict(non_blank_string),
                        },
                        "semantic_should_avoid": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 4,
                            "items": dict(non_blank_string),
                        },
                        "shot_intent": dict(non_blank_string),
                        "role": {
                            "type": "string",
                            "enum": ["hook", "body", "payoff"],
                        },
                        "stock_query_en": dict(non_blank_string),
                        "stock_query_alt_en": {"type": "string", "maxLength": 260},
                        "display_text_ar": dict(non_blank_string),
                        "source_preference": {
                            "type": "string",
                            "enum": ["stock_motion", "stock_still", "ai_still"],
                        },
                    },
                    "required": [
                        "id",
                        "section_id",
                        "viewer_intent",
                        "meaning_target",
                        "semantic_must_have",
                        "semantic_should_avoid",
                        "shot_intent",
                        "role",
                        "stock_query_en",
                        "display_text_ar",
                        "source_preference",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["visual_world", "story_arc", "retention_thread", "beats"],
        "additionalProperties": False,
    }
    plan_properties: dict[str, Any] = {
        "title": planning_string(140),
        "promise": planning_string(240),
        "cta": cta_schema,
        "sections": {
            "type": "array",
            "items": section_schema,
            "minItems": min_sections,
            "maxItems": max_sections,
        },
    }
    plan_required = ["title", "promise", "cta", "sections"]
    # Planning owns the unified visual story. The schema used to construct it
    # above must be part of the provider response contract; otherwise strict
    # providers are forced to omit visual_story and the local fallback cannot
    # satisfy the five-beat Short house cut.
    if fmt == "short":
        visual_story_schema["properties"]["beats"]["minItems"] = 5
        visual_story_schema["properties"]["beats"]["maxItems"] = 5
    plan_properties["visual_story"] = visual_story_schema
    plan_required.append("visual_story")
    if fmt == "short":
        plan_properties["practical_action_ar"] = {
            "type": "string",
            "minLength": 3,
            "maxLength": 240,
            "pattern": r"\S",
        }
        plan_required.append("practical_action_ar")
    if long_form:
        plan_properties["narrative_format"] = {
            "type": "string",
            "enum": [
                "direct_cinematic",
                "question_answer",
                "dialogue_qa",
                "inner_dialogue",
                "problem_reveal_solution",
                "story_analysis",
                "paradox",
                "hypothesis_test",
                "connected_list",
            ],
        }
        plan_required.append("narrative_format")
    return {
        "type": "object",
        "properties": plan_properties,
        "required": plan_required,
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

    short_s3_id = (
        section_ids[-1]
        if len(section_ids) == 3 and str(plan.get("practical_action_ar") or "").strip()
        else ""
    )
    section_schemas = []
    for section_id in section_ids:
        if section_id == short_s3_id:
            section_schemas.append(
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "const": section_id},
                        "s3_payoff": {"type": "string", "minLength": 20},
                    },
                    "required": ["id", "s3_payoff"],
                    "additionalProperties": False,
                }
            )
        else:
            section_schemas.append(
                {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "const": section_id},
                        "narration": {"type": "string", "minLength": 20},
                    },
                    "required": ["id", "narration"],
                    "additionalProperties": False,
                }
            )
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
    properties: dict[str, Any] = {
        "title": {"type": "string"},
        "promise": {"type": "string"},
        "cta": {"type": "string"},
        "sections": {
            "type": "array",
            "items": section_schema,
            "minItems": int(source["properties"]["sections"]["minItems"]),
            "maxItems": int(source["properties"]["sections"]["maxItems"]),
        },
    }
    required = ["title", "promise", "cta", "sections"]
    if "visual_story" in source["properties"]:
        properties["visual_story"] = source["properties"]["visual_story"]
        # Groq strict mode requires every property to be required. Preserve the
        # optional authored alternate as nullable, rather than reopen HTTP 400.
        beat_schema = properties["visual_story"]["properties"]["beats"]["items"]
        beat_schema["properties"]["stock_query_alt_en"] = {"type": ["string", "null"]}
        beat_schema["required"].append("stock_query_alt_en")
        required.append("visual_story")
    if "practical_action_ar" in source["properties"]:
        properties["practical_action_ar"] = dict(source["properties"]["practical_action_ar"])
        required.append("practical_action_ar")
    if "narrative_format" in source["properties"]:
        properties["narrative_format"] = dict(source["properties"]["narrative_format"])
        required.append("narrative_format")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _groq_script_response_schema(prompt: str) -> dict[str, Any]:
    """Strict Groq schema for script shape; exact order/semantics stay local."""
    source = _mistral_script_response_schema(prompt)
    section_ids = [
        str(item["properties"]["id"]["const"])
        for item in source["properties"]["sections"]["prefixItems"]
    ]
    source_items = source["properties"]["sections"]["prefixItems"]
    has_short_payoff = any("s3_payoff" in item["properties"] for item in source_items)
    section_properties = {
        "id": {"type": "string", "enum": section_ids},
        "narration": {"type": "string"},
    }
    if has_short_payoff:
        # Groq's response schema uses one reusable item shape rather than
        # prefixItems. Permit the one Short-only payoff key here; validate_script
        # still enforces narration for s1/s2 and s3_payoff for s3 locally.
        section_properties["s3_payoff"] = {"type": "string"}
        # Run #75, Telegram: once "required" (below) was fixed to list every
        # declared property to satisfy Groq's structural constraint, Groq then
        # enforced "s3_payoff" as present on EVERY section, including s1/s2 --
        # HTTP 400 "'/sections/0' ... missing properties: 's3_payoff'" -- because
        # Groq applies one shared item schema to the whole array, so "required"
        # is not per-section. The actual semantic rule (only s3 carries
        # s3_payoff; s1/s2 never do) can't be expressed as a per-index
        # requirement here, so instead both optional-per-section keys are made
        # nullable: Groq can satisfy "required" (the key is present) while
        # still passing null for a section where it does not semantically
        # apply. validate_script already treats falsy (including None) the
        # same as absent via `raw.get(...) or ""`, so null round-trips safely.
        section_properties["narration"] = {"type": ["string", "null"]}
        section_properties["s3_payoff"] = {"type": ["string", "null"]}
    # Groq's strict json_schema mode requires every key declared in `properties`
    # to also appear in `required` (Run #72, Telegram: Groq rejected this schema
    # with HTTP 400 "the following properties must be listed in required:
    # narration, s3_payoff" because the Short-payoff branch only listed "id").
    # The actual semantic rule -- s1/s2 need narration, only s3 needs
    # s3_payoff, never both -- is enforced locally by validate_script, same as
    # the comment above already states; `required` here only has to satisfy
    # Groq's schema-shape constraint, not express that per-section semantics.
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


_GEMINI_JSON_SCHEMA_SUPPORTED_KEYS = frozenset({
    "$id",
    "$defs",
    "$ref",
    "$anchor",
    "type",
    "format",
    "title",
    "description",
    "enum",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
    "properties",
    "additionalProperties",
    "required",
})


def _gemini_compatible_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip Mistral-only JSON Schema keywords before sending to Gemini."""
    output: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "const":
            output["enum"] = [value]
            continue
        if key not in _GEMINI_JSON_SCHEMA_SUPPORTED_KEYS:
            continue
        if key == "properties" and isinstance(value, dict):
            output[key] = {
                str(name): _gemini_compatible_json_schema(child)
                for name, child in value.items()
                if isinstance(child, dict)
            }
        elif key == "$defs" and isinstance(value, dict):
            output[key] = {
                str(name): _gemini_compatible_json_schema(child)
                for name, child in value.items()
                if isinstance(child, dict)
            }
        elif key == "items" and isinstance(value, dict):
            output[key] = _gemini_compatible_json_schema(value)
        elif key == "prefixItems" and isinstance(value, list):
            output[key] = [
                _gemini_compatible_json_schema(child)
                for child in value
                if isinstance(child, dict)
            ]
        elif key in {"anyOf", "oneOf"} and isinstance(value, list):
            output[key] = [
                _gemini_compatible_json_schema(child)
                for child in value
                if isinstance(child, dict)
            ]
        else:
            output[key] = value
    return output


def _gemini_planning_response_schema(prompt: str) -> dict[str, Any]:
    """Small Gemini Planning shape; semantic depth stays in the local validator.

    Gemini structured output can reject overly large/deep schemas with HTTP 400.
    Keep the fields that prevent visual_story omission while avoiding duplication
    of the full local visual-story validator in the wire schema.
    """
    source = _mistral_planning_response_schema(prompt)
    source_sections = source["properties"]["sections"]
    section_source = source_sections["items"]
    section_properties = {
        str(name): {"type": "string"}
        for name in section_source["properties"]
    }
    section_schema = {
        "type": "object",
        "properties": section_properties,
        "required": list(section_source["required"]),
        "additionalProperties": False,
    }

    story_source = source["properties"]["visual_story"]
    beats_source = story_source["properties"]["beats"]
    visual_story_schema = {
        "type": "object",
        "properties": {
            "visual_world": {"type": "string"},
            "story_arc": {"type": "object", "additionalProperties": True},
            "retention_thread": {"type": "object", "additionalProperties": True},
            "beats": {
                "type": "array",
                "minItems": int(beats_source["minItems"]),
                "maxItems": int(beats_source["maxItems"]),
                "items": {"type": "object", "additionalProperties": True},
            },
        },
        "required": ["visual_world", "story_arc", "retention_thread", "beats"],
        "additionalProperties": False,
    }

    properties: dict[str, Any] = {
        "title": {"type": "string"},
        "promise": {"type": "string"},
        "cta": {"type": "string"},
        "sections": {
            "type": "array",
            "items": section_schema,
            "minItems": int(source_sections["minItems"]),
            "maxItems": int(source_sections["maxItems"]),
        },
        "visual_story": visual_story_schema,
    }
    required = ["title", "promise", "cta", "sections", "visual_story"]
    for name in ("practical_action_ar", "narrative_format"):
        if name in source["properties"]:
            properties[name] = _gemini_compatible_json_schema(
                source["properties"][name]
            )
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _gemini_stage_call(prompt: str, max_tokens: int, stage: str) -> dict[str, Any]:
    if stage == "planning":
        return _gemini_call(
            prompt,
            max_tokens,
            response_schema=_gemini_planning_response_schema(prompt),
        )
    if stage == "script_patch":
        return _gemini_call(
            prompt,
            max_tokens,
            response_schema=_gemini_compatible_json_schema(
                MISTRAL_SCRIPT_PATCH_SCHEMA
            ),
        )
    return _gemini_call(prompt, max_tokens)


def _gemini_flash_lite_stage_call(
    prompt: str, max_tokens: int, stage: str
) -> dict[str, Any]:
    if stage == "planning":
        return _gemini_flash_lite_call(
            prompt,
            max_tokens,
            response_schema=_gemini_planning_response_schema(prompt),
        )
    if stage == "script_patch":
        return _gemini_flash_lite_call(
            prompt,
            max_tokens,
            response_schema=_gemini_compatible_json_schema(
                MISTRAL_SCRIPT_PATCH_SCHEMA
            ),
        )
    return _gemini_flash_lite_call(prompt, max_tokens)


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
    if stage == "script_patch":
        return _groq_call(
            prompt,
            max_tokens,
            # Groq strict mode needs only the response shape here. The local
            # patch validator owns the 400/550 character safety bounds, so do
            # not send Mistral-only minLength/maxLength keywords over the wire.
            response_schema=_gemini_compatible_json_schema(
                MISTRAL_SCRIPT_PATCH_SCHEMA
            ),
            schema_name="script_patch",
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
    # Per-provider admission ceiling, tighter than the Runner-wide MAX_PROMPT_BYTES.
    # None means "no provider-specific ceiling beyond the global one".
    max_prompt_utf8_bytes: int | None = None
    # Optional tighter ceilings for stages whose request/schema overhead differs.
    max_prompt_utf8_bytes_by_stage: Mapping[str, int] | None = None

    def invoke(self, prompt: str, max_tokens: int, stage: str) -> dict[str, Any]:
        if self.accepts_stage:
            return self.call(prompt, max_tokens, stage)
        return self.call(prompt, max_tokens)


def default_adapters() -> tuple[ProviderAdapter, ...]:
    return (
        ProviderAdapter("gemini", _gemini_stage_call, accepts_stage=True),
        ProviderAdapter(
            "gemini_flash_lite",
            _gemini_flash_lite_stage_call,
            accepts_stage=True,
        ),
        ProviderAdapter(
            "groq",
            _groq_stage_call,
            accepts_stage=True,
            max_prompt_utf8_bytes=GROQ_MAX_PROMPT_UTF8_BYTES,
            max_prompt_utf8_bytes_by_stage={
                "planning": GROQ_MAX_PLANNING_PROMPT_UTF8_BYTES,
            },
        ),
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

    Narrow same-provider exceptions are capped and never create a second provider
    sweep: Mistral may re-issue malformed Planning JSON, Mistral may make one
    validator-guided Planning correction after a structurally valid response is
    rejected locally, and classic transient 502/503/504 or transport failures may
    receive one short delayed retry (see _is_transient_wire_failure). A provider
    that still fails its bounded retry is marked failed and routing ends or moves
    to the next eligible adapter exactly as before.
    """

    def __init__(self, adapters: Iterable[ProviderAdapter] | None = None) -> None:
        self.adapters = tuple(adapters or default_adapters())
        if not self.adapters:
            raise ValueError("at least one provider adapter is required")
        self.events: list[dict[str, Any]] = []
        # Per-run only. A 429 with no short Retry-After (or a long one) is
        # treated as unavailable for the remaining stages of this pipeline run.
        # This avoids burning the same exhausted free-tier provider repeatedly.
        self._rate_limited_for_run: set[str] = set()

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
        detail: str | None = None,
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
                # Bounded provider-sent error text (never the outbound prompt or an
                # API key) - None when the provider gave nothing to show, including
                # for every non-failure or local/validator event.
                "detail": detail,
            }
        )

    def route_exact_provider(
        self,
        *,
        provider_name: str,
        stage: str,
        prompt: str,
        max_tokens: int,
        validator: Callable[[Any], dict[str, Any]],
    ) -> dict[str, Any]:
        adapter = next(
            (
                item for item in self.adapters
                if item.name == provider_name
                and (item.stages is None or stage in item.stages)
            ),
            None,
        )
        if adapter is None:
            raise RuntimeError(
                f"{stage} exact provider unavailable: {provider_name or 'unknown'}"
            )
        provider_prompt = _provider_prompt(
            prompt,
            provider=adapter.name,
            stage=stage,
        )
        try:
            candidate = adapter.invoke(provider_prompt, max_tokens, stage)
        except Exception as exc:
            reason = str(getattr(exc, "reason_code", "provider_failure"))
            self._event(
                stage=stage,
                provider=adapter.name,
                result="failed",
                wire_attempted=True,
                reason=reason,
                provider_attempt=1,
                stage_wire_attempt=1,
                detail=getattr(exc, "error_detail", None),
            )
            raise
        try:
            normalized = validator(candidate)
        except Exception as exc:
            detail = None
            if stage == "visual_query_recovery" and isinstance(candidate, dict):
                query = str(candidate.get("alternate_query") or "").strip()
                detail = json.dumps(
                    {
                        "alternate_query_chars": len(query),
                        "alternate_query_words": len(query.split()),
                        "alternate_query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
                    },
                    separators=(",", ":"),
                )
            self._event(
                stage=stage,
                provider=adapter.name,
                result="invalid_output",
                wire_attempted=True,
                reason=_safe_validator_reason(exc),
                provider_attempt=1,
                stage_wire_attempt=1,
                detail=detail,
            )
            raise
        self._event(
            stage=stage,
            provider=adapter.name,
            result="success",
            wire_attempted=True,
            reason=None,
            provider_attempt=1,
            stage_wire_attempt=1,
        )
        return normalized

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
            if adapter.name in self._rate_limited_for_run:
                failures.append(f"{adapter.name}:rate_limit_cached")
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="unavailable",
                    wire_attempted=False,
                    reason="rate_limit_cached",
                    provider_attempt=None,
                    stage_wire_attempt=None,
                )
                continue

            stage_prompt_limit = adapter.max_prompt_utf8_bytes
            if adapter.max_prompt_utf8_bytes_by_stage is not None:
                stage_prompt_limit = adapter.max_prompt_utf8_bytes_by_stage.get(
                    stage, stage_prompt_limit
                )
            if stage_prompt_limit is not None:
                admission_prompt_bytes = len(
                    _provider_prompt(prompt, provider=adapter.name, stage=stage).encode(
                        "utf-8"
                    )
                )
                if admission_prompt_bytes > stage_prompt_limit:
                    failures.append(f"{adapter.name}:prompt_too_large_for_provider")
                    self._event(
                        stage=stage,
                        provider=adapter.name,
                        result="unavailable",
                        wire_attempted=False,
                        reason="prompt_too_large_for_provider",
                        provider_attempt=None,
                        stage_wire_attempt=None,
                        detail=(
                            f"prompt_bytes={admission_prompt_bytes} "
                            f"limit={stage_prompt_limit}"
                        ),
                    )
                    continue

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

                    transient_retry = (
                        provider_attempt == 1 and _is_transient_wire_failure(exc)
                    )
                    if transient_retry:
                        self._event(
                            stage=stage,
                            provider=adapter.name,
                            result="retrying",
                            wire_attempted=True,
                            reason="transient_5xx_retry",
                            provider_attempt=provider_attempt,
                            stage_wire_attempt=wire_count,
                        )
                        time.sleep(TRANSIENT_RETRY_DELAY_SECONDS)
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
                        detail=getattr(exc, "error_detail", None),
                    )
                    retry_after = getattr(exc, "retry_after_seconds", None)
                    if getattr(exc, "http_status", None) == 429:
                        short_retry = (
                            stage in SHORT_RETRY_AFTER_STAGES
                            and isinstance(retry_after, (int, float))
                            and 0 < float(retry_after) <= MAX_SHORT_RETRY_AFTER_SECONDS
                        )
                        if short_retry and adapter_index + 1 < len(eligible_adapters):
                            time.sleep(float(retry_after))
                        elif not short_retry and not _is_transient_429_detail(
                            getattr(exc, "error_detail", None)
                        ):
                            self._rate_limited_for_run.add(adapter.name)
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
                if getattr(exc, "terminal_provider_fallback", False):
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
                    # Host-owned Short action mutations are deterministic local
                    # contract violations, not a reason to spend another free-tier
                    # provider call trying the same forbidden edit again.
                    raise
                retry_prompt = None
                retry_event_reason = None
                if adapter.name == "mistral" and stage == "planning":
                    retry_prompt = _mistral_planning_validator_retry_prompt(
                        provider_prompt, exc
                    )
                    if retry_prompt is not None:
                        retry_event_reason = "mistral_planning_validator_retry"
                elif adapter.name == "mistral" and stage == "script":
                    retry_prompt = _mistral_short_hook_validator_retry_prompt(
                        provider_prompt, exc
                    )
                    if retry_prompt is not None:
                        retry_event_reason = "mistral_short_hook_validator_retry"
                elif adapter.name == "mistral" and stage == "script_patch":
                    retry_prompt = _mistral_script_patch_validator_retry_prompt(
                        provider_prompt, exc
                    )
                    if retry_prompt is not None:
                        retry_event_reason = "mistral_script_patch_validator_retry"

                validator_retries_used = 0
                abandon_provider = False
                while retry_prompt is not None:
                    self._event(
                        stage=stage,
                        provider=adapter.name,
                        result="retrying",
                        wire_attempted=True,
                        reason=retry_event_reason,
                        provider_attempt=provider_attempt,
                        stage_wire_attempt=wire_count,
                    )
                    retry_attempt = provider_attempt + 1
                    try:
                        retry_candidate = adapter.invoke(retry_prompt, max_tokens, stage)
                    except NoWireFailure as retry_exc:
                        failures.append(f"{adapter.name}:{retry_exc.reason_code}")
                        self._event(
                            stage=stage,
                            provider=adapter.name,
                            result="unavailable",
                            wire_attempted=False,
                            reason=retry_exc.reason_code,
                            provider_attempt=None,
                            stage_wire_attempt=None,
                        )
                        abandon_provider = True
                        break
                    except Exception as retry_exc:
                        wire_count += 1
                        retry_reason = str(
                            getattr(retry_exc, "reason_code", "provider_failure")
                        )
                        failures.append(f"{adapter.name}:{retry_reason}")
                        self._event(
                            stage=stage,
                            provider=adapter.name,
                            result="failed",
                            wire_attempted=True,
                            reason=retry_reason,
                            provider_attempt=retry_attempt,
                            stage_wire_attempt=wire_count,
                        )
                        abandon_provider = True
                        break
                    else:
                        wire_count += 1
                        provider_attempt = retry_attempt
                        try:
                            normalized = validator(retry_candidate)
                        except Exception as retry_exc:
                            exc = retry_exc
                            validator_retries_used += 1
                            retry_prompt = None
                            if (
                                adapter.name == "mistral"
                                and stage == "planning"
                                and validator_retries_used
                                < MISTRAL_PLANNING_MAX_VALIDATOR_RETRIES
                            ):
                                retry_prompt = _mistral_planning_validator_retry_prompt(
                                    provider_prompt, exc
                                )
                            if retry_prompt is None:
                                break
                            continue
                        else:
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

                if abandon_provider:
                    continue
                if adapter.name == "mistral" and stage == "planning":
                    raw_content = mistral_executor.get_last_mistral_executor_raw_content()
                    print(
                        "Mistral planning validator rejected raw content: "
                        + json.dumps(
                            _safe_mistral_planning_raw_diagnostic(raw_content, exc),
                            ensure_ascii=True,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                elif adapter.name == "mistral" and stage == "visual_query_recovery":
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
                validator_detail = None
                if stage in {"planning", "script_patch"}:
                    message = " ".join(str(exc).split()).strip()[:500]
                    if message:
                        validator_detail = json.dumps(
                            {
                                "validator_error_type": type(exc).__name__,
                                "validator_error": message,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        )
                self._event(
                    stage=stage,
                    provider=adapter.name,
                    result="invalid_output",
                    wire_attempted=True,
                    reason=reason,
                    provider_attempt=provider_attempt,
                    stage_wire_attempt=wire_count,
                    detail=validator_detail,
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
