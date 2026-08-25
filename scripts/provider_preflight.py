from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import requests


DEFAULT_TIMEOUT_SECONDS = 20
STOCK_REQUIRED_HEADROOM = 24
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GROQ_MODELS_URL = "https://api.groq.com/openai/v1/models"
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/v1/videos/search"
PIXABAY_VIDEO_SEARCH_URL = "https://pixabay.com/api/videos/"
USER_AGENT = "isco-video-preflight/3"


@dataclass(frozen=True)
class ProviderCheck:
    provider: str
    status: str
    http_status: int | None = None
    detail: str | None = None
    rate_limit_limit: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset: str | None = None
    required_headroom: int | None = None
    headroom_ok: bool | None = None


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


def _header(response: requests.Response, name: str) -> str | None:
    wanted = name.casefold()
    for key, value in response.headers.items():
        if str(key).casefold() == wanted:
            text = str(value).strip()
            return text or None
    return None


def _header_int(response: requests.Response, name: str) -> int | None:
    raw = _header(response, name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"provider readiness returned invalid {name} header") from exc
    if value < 0:
        raise RuntimeError(f"provider readiness returned negative {name} header")
    return value


def _stock_capacity(provider: str, response: requests.Response) -> tuple[int | None, int, str | None]:
    limit = _header_int(response, "X-RateLimit-Limit")
    remaining = _header_int(response, "X-RateLimit-Remaining")
    reset = _header(response, "X-RateLimit-Reset")
    if remaining is None:
        raise RuntimeError(f"{provider} readiness could not prove rate-limit remaining headroom")
    if remaining < STOCK_REQUIRED_HEADROOM:
        raise RuntimeError(
            f"{provider} readiness headroom too low: remaining={remaining}, required={STOCK_REQUIRED_HEADROOM}"
        )
    return limit, remaining, reset


def _gemini_model_names(api_key: str, *, timeout: int) -> set[str]:
    names: set[str] = set()
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
        names.update(
            str(item.get("name") or "").removeprefix("models/")
            for item in models
            if isinstance(item, dict) and item.get("name")
        )
        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token:
            return names
        if next_token in seen_tokens:
            raise RuntimeError("gemini models pagination repeated a page token")
        seen_tokens.add(next_token)
        page_token = next_token
    raise RuntimeError("gemini models pagination exceeded safety bound")


def check_gemini(api_key: str, *, content_model: str, tts_model: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    names = _gemini_model_names(api_key, timeout=timeout)
    aliases = {
        content_model: {content_model, "gemini-3.5-flash-lite"} if content_model == "gemini-2.5-flash" else {content_model},
        tts_model: {tts_model},
    }
    missing = [requested for requested, accepted in aliases.items() if not (accepted & names)]
    if missing:
        raise RuntimeError("gemini configured model unavailable: " + ", ".join(missing))
    return ProviderCheck("gemini", "pass", 200, "credential and configured models available")


def check_groq(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    response = requests.get(
        GROQ_MODELS_URL,
        headers={"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    _require_ok("groq", response)
    payload = _json_object("groq", response)
    if not isinstance(payload.get("data"), list):
        raise RuntimeError("groq models response has invalid data field")
    return ProviderCheck("groq", "pass", response.status_code, "credential accepted and models endpoint healthy")


def check_openrouter(api_key: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> ProviderCheck:
    response = requests.get(
        OPENROUTER_KEY_URL,
        headers={"Authorization": "Bearer " + api_key, "User-Agent": USER_AGENT},
        timeout=timeout,
    )
    _require_ok("openrouter", response)
    payload = _json_object("openrouter", response)
    if not isinstance(payload.get("data"), dict):
        raise RuntimeError("openrouter key response has invalid data field")
    return ProviderCheck("openrouter", "pass", response.status_code, "credential accepted")


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
    limit, remaining, reset = _stock_capacity("pexels", response)
    return ProviderCheck(
        "pexels", "pass", response.status_code,
        "credential, endpoint and production headroom available",
        limit, remaining, reset, STOCK_REQUIRED_HEADROOM, True,
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
    limit, remaining, reset = _stock_capacity("pixabay", response)
    return ProviderCheck(
        "pixabay", "pass", response.status_code,
        "credential, endpoint and production headroom available",
        limit, remaining, reset, STOCK_REQUIRED_HEADROOM, True,
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
    failure: BaseException | None = None
    for provider, fn in checks:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(ProviderCheck(provider, "block", None, _safe_failure_detail(exc)))
            failure = failure or exc
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.write_text(
        json.dumps({"schema_version": 3, "checks": [asdict(item) for item in results]}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(output)
    if failure is not None:
        raise RuntimeError("provider readiness preflight failed; see provider-preflight.json") from None
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
    run_preflight(
        gemini_key=_read_secret(args.gemini_key_file),
        groq_key=_read_secret(args.groq_key_file),
        openrouter_key=_read_secret(args.openrouter_key_file),
        pexels_key=_read_secret(args.pexels_key_file),
        pixabay_key=_read_secret(args.pixabay_key_file),
        content_model=args.content_model,
        tts_model=args.tts_model,
        output=args.output,
    )
    print("Provider readiness preflight PASS: gemini, groq, openrouter, pexels, pixabay; stock headroom proven")


if __name__ == "__main__":
    main()
