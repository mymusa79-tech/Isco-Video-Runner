from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests


DEFAULT_TIMEOUT_SECONDS = 20
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"
USER_AGENT = "isco-video-preflight/4"

# This must mirror the Engine pin's provider resolver. The workflow passes the
# policy-facing model name, while the pinned Engine resolves it before every real
# content/Vision request. Preflight must certify the same network model, never a
# convenient alias that production will not actually call.
GEMINI_RUNTIME_CONTENT_ALIASES = {
    "gemini-2.5-flash": "gemini-3.5-flash-lite",
}

# Current V4 runtime topology. Gemini is still used directly for Vision/editorial
# work and Pexels search errors are not isolated before Pixabay is tried, so those
# two are hard readiness dependencies today. Groq/OpenRouter/Pixabay are genuine
# fallbacks: their loss must be visible in diagnostics, not promoted into a false
# whole-run veto while the hard path remains healthy.
REQUIRED_PROVIDERS = frozenset({"gemini", "pexels"})
FALLBACK_PROVIDERS = frozenset({"groq", "openrouter", "pixabay"})
GROQ_RUNTIME_MODEL = "openai/gpt-oss-20b"


@dataclass(frozen=True)
class ProviderCheck:
    provider: str
    status: str
    http_status: int | None = None
    detail: str | None = None
    capacity_status: str = "unknown"
    capacity_remaining: float | int | None = None
    capacity_unit: str | None = None
    capacity_reset: str | None = None


def _read_secret(path: str | Path) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("provider credential file is empty")
    return value


def _require_ok(provider: str, response: requests.Response) -> None:
    if response.ok:
        return
    status = int(response.status_code)
    if status in {401, 403}:
        raise RuntimeError(f"{provider} credential rejected: HTTP {status}")
    if status == 429:
        raise RuntimeError(f"{provider} readiness blocked by rate/quota limit: HTTP 429")
    if status == 498:
        raise RuntimeError(f"{provider} readiness blocked by provider capacity: HTTP 498")
    if 500 <= status <= 599:
        raise RuntimeError(f"{provider} readiness blocked by upstream outage: HTTP {status}")
    raise RuntimeError(f"{provider} readiness check failed: HTTP {status}")


def _safe_failure_detail(exc: BaseException) -> str:
    if isinstance(exc, RuntimeError):
        return str(exc)[:300]
    if isinstance(exc, requests.Timeout):
        return "provider readiness request timed out"
    if isinstance(exc, requests.RequestException):
        return "provider readiness transport failure"
    return f"provider readiness malformed response ({type(exc).__name__})"


def _json_object(provider: str, response: requests.Response) -> dict:
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"{provider} readiness returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider} readiness returned non-object JSON")
    return payload


def _header_number(provider: str, response: requests.Response, name: str, *, integer: bool = False) -> float | int:
    raw = str(response.headers.get(name) or "").strip()
    if not raw:
        raise RuntimeError(f"{provider} capacity evidence missing required {name} header")
    try:
        value = int(raw) if integer else float(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{provider} capacity evidence has invalid {name} header") from exc
    if value < 0:
        raise RuntimeError(f"{provider} capacity evidence has negative {name} header")
    return value


def _positive_header_capacity(
    provider: str,
    response: requests.Response,
    *,
    remaining_header: str,
    unit: str,
    reset_header: str | None = None,
) -> tuple[float | int, str | None]:
    remaining = _header_number(provider, response, remaining_header, integer=True)
    if remaining <= 0:
        raise RuntimeError(f"{provider} readiness blocked: no remaining {unit} capacity")
    reset = None
    if reset_header:
        raw = str(response.headers.get(reset_header) or "").strip()
        if not raw:
            raise RuntimeError(f"{provider} capacity evidence missing required {reset_header} header")
        reset = raw[:120]
    return remaining, reset


def _gemini_models(api_key: str, *, timeout: int) -> dict[str, set[str]]:
    models_by_name: dict[str, set[str]] = {}
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(10):
        params: dict[str, object] = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        response = requests.get(
            GEMINI_MODELS_URL,
            headers={"x-goog-api-key": api_key, "User-Agent": USER_AGENT},
            params=params,
            timeout=timeout,
        )
        _require_ok("gemini", response)
        payload = _json_object("gemini", response)
        models = payload.get("models", [])
        if not isinstance(models, list):
            raise RuntimeError("gemini models response has invalid models field")
        for item in models:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"]).removeprefix("models/")
            methods = item.get("supportedGenerationMethods", [])
            if not isinstance(methods, list):
                raise RuntimeError(f"gemini model metadata malformed for {name}")
            models_by_name[name] = {str(method) for method in methods}
        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token:
            return models_by_name
        if next_token in seen_tokens:
            raise RuntimeError("gemini models pagination repeated a page token")
        seen_tokens.add(next_token)
        page_token = next_token
    raise RuntimeError("gemini models pagination exceeded safety bound")


def _gemini_runtime_content_model(requested_model: str) -> str:
    return GEMINI_RUNTIME_CONTENT_ALIASES.get(requested_model, requested_model)


def check_gemini(api_key: str, *, content_model: str, tts_model: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    """Certify the exact Gemini network model used by the pinned Engine.

    Run 111 showed why countTokens is the wrong readiness gate: it is a tokenizer
    endpoint, not inference-capacity evidence, and its 404 can disagree with the
    actual model-discovery/runtime path. Google exposes supportedGenerationMethods in
    models.list, so preflight now verifies the exact resolved content model and its
    generateContent capability without spending an inference attempt.

    Gemini TTS is not a hard whole-run dependency because canonical V4 separately
    certifies Piper and the TTS budget owns Gemini -> Piper fallback. Its availability
    is still reported so degraded cloud voice is observable before production.
    """
    models = _gemini_models(api_key, timeout=timeout)
    resolved_content = _gemini_runtime_content_model(content_model)
    if resolved_content not in models:
        raise RuntimeError(
            f"gemini runtime content model unavailable: requested={content_model} resolved={resolved_content}"
        )
    methods = models[resolved_content]
    if "generateContent" not in methods:
        raise RuntimeError(
            f"gemini runtime content model lacks generateContent support: {resolved_content}"
        )

    tts_available = tts_model in models
    tts_note = (
        f"TTS model {tts_model} listed"
        if tts_available
        else f"TTS model {tts_model} not listed; canonical Piper fallback required"
    )
    return ProviderCheck(
        "gemini",
        "pass",
        200,
        (
            f"credential accepted; exact runtime content model {resolved_content} supports generateContent; "
            f"{tts_note}; dynamic inference capacity is provider-controlled"
        ),
        "dynamic_unobservable",
        None,
        "inference quota",
        None,
    )


def _groq_model_ids(payload: dict) -> set[str]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("groq models response has invalid data field")
    return {
        str(item.get("id") or "").strip()
        for item in data
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }


def _optional_positive_header(provider: str, response: requests.Response, name: str) -> int | None:
    raw = str(response.headers.get(name) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{provider} capacity evidence has invalid {name} header") from exc
    if value <= 0:
        raise RuntimeError(f"{provider} readiness blocked: no remaining capacity in {name}")
    return value


def check_groq(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    """Verify auth + exact fallback model, using capacity headers only when observable.

    Groq documents rate-limit headers on API responses, but Run 111 demonstrated that
    the /models discovery response can omit them. Missing discovery headers therefore
    mean capacity is unobservable at zero inference cost; explicit zero/negative or
    malformed capacity evidence still blocks this fallback provider.
    """
    response = requests.get(
        GROQ_MODELS_URL,
        headers={"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    _require_ok("groq", response)
    payload = _json_object("groq", response)
    model_ids = _groq_model_ids(payload)
    if GROQ_RUNTIME_MODEL not in model_ids:
        raise RuntimeError(f"groq configured fallback model unavailable: {GROQ_RUNTIME_MODEL}")

    remaining_requests = _optional_positive_header("groq", response, "x-ratelimit-remaining-requests")
    remaining_tokens = _optional_positive_header("groq", response, "x-ratelimit-remaining-tokens")
    reset = str(response.headers.get("x-ratelimit-reset-requests") or "").strip()[:120] or None

    if remaining_requests is None and remaining_tokens is None:
        return ProviderCheck(
            "groq",
            "pass",
            response.status_code,
            f"credential and fallback model {GROQ_RUNTIME_MODEL} available; discovery response exposes no capacity headers",
            "dynamic_unobservable",
            None,
            "rate-limit headroom",
            reset,
        )

    visible: list[str] = []
    if remaining_requests is not None:
        visible.append(f"{remaining_requests} requests")
    if remaining_tokens is not None:
        visible.append(f"{remaining_tokens} tokens")
    return ProviderCheck(
        "groq",
        "pass",
        response.status_code,
        f"credential and fallback model available; positive visible headroom ({', '.join(visible)})",
        "positive",
        remaining_requests if remaining_requests is not None else remaining_tokens,
        "requests" if remaining_requests is not None else "tokens",
        reset,
    )


def _parse_expiry(provider: str, value: object) -> None:
    text = str(value or "").strip()
    if not text:
        return
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{provider} key expiry is malformed") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed <= datetime.now(timezone.utc):
        raise RuntimeError(f"{provider} key is expired")


def check_openrouter(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    response = requests.get(
        OPENROUTER_KEY_URL,
        headers={"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    _require_ok("openrouter", response)
    payload = _json_object("openrouter", response)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("openrouter key response has invalid data field")
    if data.get("is_management_key") is True or data.get("is_provisioning_key") is True:
        raise RuntimeError("openrouter key is administrative/provisioning, not an inference key")
    _parse_expiry("openrouter", data.get("expires_at"))

    limit = data.get("limit")
    remaining = data.get("limit_remaining")
    if limit is None:
        capacity_status = "unbounded_key_limit"
        capacity_remaining = None
        detail = "credential accepted; no key-level spend cap is configured"
    else:
        try:
            numeric_limit = float(limit)
            numeric_remaining = float(remaining)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("openrouter key capacity fields are malformed") from exc
        if numeric_limit < 0 or numeric_remaining <= 0:
            raise RuntimeError("openrouter readiness blocked: key spend capacity exhausted")
        capacity_status = "positive"
        capacity_remaining = numeric_remaining
        detail = f"credential accepted; positive key spend headroom ({numeric_remaining:g} credits remaining)"
    return ProviderCheck(
        "openrouter",
        "pass",
        response.status_code,
        detail,
        capacity_status,
        capacity_remaining,
        "credits",
        str(data.get("limit_reset") or "")[:120] or None,
    )


def check_pexels(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    response = requests.get(
        PEXELS_VIDEO_SEARCH_URL,
        headers={"Authorization": api_key, "User-Agent": USER_AGENT},
        params={"query": "nature", "per_page": 1, "orientation": "landscape", "size": "medium", "locale": "en-US"},
        timeout=timeout,
    )
    _require_ok("pexels", response)
    payload = _json_object("pexels", response)
    if not isinstance(payload.get("videos"), list):
        raise RuntimeError("pexels video-search response has invalid videos field")
    remaining, reset = _positive_header_capacity(
        "pexels",
        response,
        remaining_header="X-Ratelimit-Remaining",
        unit="monthly requests",
        reset_header="X-Ratelimit-Reset",
    )
    return ProviderCheck(
        "pexels",
        "pass",
        response.status_code,
        f"credential/search endpoint available; {remaining} monthly requests remain",
        "positive",
        remaining,
        "monthly requests",
        reset,
    )


def check_pixabay(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    response = requests.get(
        PIXABAY_VIDEO_SEARCH_URL,
        headers={"User-Agent": USER_AGENT},
        params={"key": api_key, "q": "nature", "per_page": 3, "safesearch": "true"},
        timeout=timeout,
    )
    _require_ok("pixabay", response)
    payload = _json_object("pixabay", response)
    if not isinstance(payload.get("hits"), list):
        raise RuntimeError("pixabay video-search response has invalid hits field")
    remaining, reset = _positive_header_capacity(
        "pixabay",
        response,
        remaining_header="X-RateLimit-Remaining",
        unit="requests in current 60-second window",
        reset_header="X-RateLimit-Reset",
    )
    return ProviderCheck(
        "pixabay",
        "pass",
        response.status_code,
        f"credential/search endpoint available; {remaining} requests remain in current rate window",
        "positive",
        remaining,
        "requests/60s window",
        reset,
    )


def run_preflight(
    *,
    gemini_key: str,
    groq_key: str,
    openrouter_key: str,
    pexels_key: str,
    pixabay_key: str,
    content_model: str,
    tts_model: str,
    output: Path,
) -> list[ProviderCheck]:
    checks: list[tuple[str, Callable[[], ProviderCheck]]] = [
        ("gemini", lambda: check_gemini(gemini_key, content_model=content_model, tts_model=tts_model)),
        ("groq", lambda: check_groq(groq_key)),
        ("openrouter", lambda: check_openrouter(openrouter_key)),
        ("pexels", lambda: check_pexels(pexels_key)),
        ("pixabay", lambda: check_pixabay(pixabay_key)),
    ]
    results: list[ProviderCheck] = []
    for provider, fn in checks:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(ProviderCheck(provider, "block", None, _safe_failure_detail(exc), "blocked"))

    by_provider = {item.provider: item for item in results}
    hard_failures = [
        provider
        for provider in sorted(REQUIRED_PROVIDERS)
        if by_provider.get(provider) is None or by_provider[provider].status != "pass"
    ]
    fallback_degraded = [
        provider
        for provider in sorted(FALLBACK_PROVIDERS)
        if by_provider.get(provider) is None or by_provider[provider].status != "pass"
    ]
    overall_status = "block" if hard_failures else "pass"

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "overall_status": overall_status,
                "required_providers": sorted(REQUIRED_PROVIDERS),
                "fallback_providers": sorted(FALLBACK_PROVIDERS),
                "hard_failures": hard_failures,
                "fallback_degraded": fallback_degraded,
                "capacity_contract": (
                    "hard runtime dependencies must pass; fallback providers are diagnostic and may degrade; "
                    "positive provider-visible headroom is enforced where observable without spending inference"
                ),
                "checks": [asdict(item) for item in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(output)
    if hard_failures:
        raise RuntimeError(
            "provider readiness/capacity preflight failed for hard runtime dependencies: "
            + ", ".join(hard_failures)
            + "; see provider-preflight.json"
        ) from None
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--content-model", default="gemini-2.5-flash")
    parser.add_argument("--tts-model", default="gemini-3.1-flash-tts-preview")
    parser.add_argument("--gemini-key-file", required=True)
    parser.add_argument("--groq-key-file", required=True)
    parser.add_argument("--openrouter-key-file", required=True)
    parser.add_argument("--pexels-key-file", required=True)
    parser.add_argument("--pixabay-key-file", required=True)
    args = parser.parse_args()
    results = run_preflight(
        gemini_key=_read_secret(args.gemini_key_file),
        groq_key=_read_secret(args.groq_key_file),
        openrouter_key=_read_secret(args.openrouter_key_file),
        pexels_key=_read_secret(args.pexels_key_file),
        pixabay_key=_read_secret(args.pixabay_key_file),
        content_model=args.content_model,
        tts_model=args.tts_model,
        output=args.output,
    )
    degraded = [item.provider for item in results if item.provider in FALLBACK_PROVIDERS and item.status != "pass"]
    suffix = f"; degraded fallbacks: {', '.join(degraded)}" if degraded else ""
    print(f"Provider readiness/capacity preflight PASS: hard runtime dependencies ready{suffix}")


if __name__ == "__main__":
    main()
