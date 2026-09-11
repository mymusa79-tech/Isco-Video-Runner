from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Production historically invoked this facade as a file.  Keep that entrypoint
# compatible even though the implementation now lives in sibling package modules.
# The canonical workflow uses ``python -m`` below, but this bootstrap prevents a
# future direct caller from recreating Run 202's package-resolution failure.
if __package__ in {None, ""}:
    _REPO_ROOT = Path(__file__).resolve().parent.parent
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))

from scripts import provider_preflight_core as _core
from scripts import quality_capability_router as quality_router
from scripts.youtube_oauth_readonly_firewall import enforce_from_runner_temp


_original_main = _core.main


@dataclass(frozen=True)
class _QualityReadinessEvidence:
    reason: str


def _critical_capability_preflight_path() -> Path:
    explicit = str(os.environ.get("ISCO_QUALITY_CAPABILITY_PREFLIGHT_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise RuntimeError(
            "critical quality capability preflight requires RUNNER_TEMP or "
            "ISCO_QUALITY_CAPABILITY_PREFLIGHT_PATH"
        )
    return Path(runner_temp) / "quality-capability-preflight.json"


def _argv_value(name: str, *, required: bool = True, default: str = "") -> str:
    try:
        index = sys.argv.index(name)
    except ValueError:
        if required:
            raise RuntimeError(f"quality capability preflight missing CLI argument: {name}")
        return default
    if index + 1 >= len(sys.argv):
        raise RuntimeError(f"quality capability preflight missing CLI value: {name}")
    return str(sys.argv[index + 1]).strip()


def _quality_candidates() -> tuple[quality_router.CapabilityCandidate, ...]:
    seen: dict[str, quality_router.CapabilityCandidate] = {}
    for policy in quality_router.capability_registry().values():
        for candidate in policy.candidates:
            seen.setdefault(candidate.identity, candidate)
    return tuple(seen.values())


def _quality_candidate_readiness() -> tuple[set[str], dict[str, str]]:
    """Certify exact model IDs with zero inference before expensive production.

    This is intentionally provider-specific preflight, not routing policy. The Router
    remains SDK/HTTP neutral. Gemini model discovery, Groq's authenticated model catalog,
    and OpenRouter's account+catalog check are reused from the established provider
    preflight owner. Readiness is model-scoped so one unavailable Gold Vision model does
    not incorrectly poison Groq Whisper for Audio QC.
    """
    candidates = _quality_candidates()
    ready: set[str] = set()
    blocked: dict[str, str] = {}

    gemini_key = _core._read_secret(_argv_value("--gemini-key-file"))
    groq_key = _core._read_secret(_argv_value("--groq-key-file"))
    openrouter_key = _core._read_secret(_argv_value("--openrouter-key-file"))
    tts_model = _argv_value(
        "--tts-model",
        required=False,
        default="gemini-3.1-flash-tts-preview",
    )

    gemini_models = sorted({item.model for item in candidates if item.provider == "gemini"})
    for model in gemini_models:
        identity = f"gemini:{model}"
        try:
            _core.check_gemini(
                gemini_key,
                content_model=model,
                tts_model=tts_model,
            )
            ready.add(identity)
        except Exception as exc:
            blocked[identity] = _core._safe_failure_detail(exc)

    groq_candidates = [item for item in candidates if item.provider == "groq"]
    if groq_candidates:
        try:
            response = _core.requests.get(
                _core.GROQ_MODELS_URL,
                headers={
                    "Authorization": "Bearer " + groq_key,
                    "User-Agent": _core.USER_AGENT,
                },
                timeout=_core.DEFAULT_TIMEOUT_SECONDS,
            )
            _core._require_ok("groq", response)
            ids = _core._model_ids("groq", _core._json_object("groq", response))
            # If discovery exposes capacity headers, explicit exhaustion is authoritative.
            _core._optional_positive_header(
                "groq", response, "x-ratelimit-remaining-requests"
            )
            _core._optional_positive_header(
                "groq", response, "x-ratelimit-remaining-tokens"
            )
            for candidate in groq_candidates:
                if candidate.model in ids:
                    ready.add(candidate.identity)
                else:
                    blocked[candidate.identity] = (
                        f"groq exact quality model unavailable: {candidate.model}"
                    )
        except Exception as exc:
            reason = _core._safe_failure_detail(exc)
            for candidate in groq_candidates:
                blocked[candidate.identity] = reason

    # OpenRouter emergency models are certified independently. A delisted optional model
    # excludes only that exact candidate; another pinned emergency model remains usable.
    for candidate in (item for item in candidates if item.provider == "openrouter"):
        try:
            _core.check_openrouter(
                openrouter_key,
                configured_models=(candidate.model,),
            )
            ready.add(candidate.identity)
        except Exception as exc:
            blocked[candidate.identity] = _core._safe_failure_detail(exc)

    return ready, blocked


def _write_quality_preflight(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _run_critical_quality_preflight() -> dict:
    ready, blocked = _quality_candidate_readiness()

    def credential_probe(candidate: quality_router.CapabilityCandidate) -> bool:
        # Exact authenticated catalog checks above are stronger than merely seeing an
        # environment variable, and they use the same CLI key files as provider preflight.
        return candidate.identity in ready or candidate.identity in blocked

    def health_probe(candidate: quality_router.CapabilityCandidate):
        reason = blocked.get(candidate.identity)
        return _QualityReadinessEvidence(reason) if reason else None

    result = quality_router.critical_capacity_preflight(
        credential_probe=credential_probe,
        health_probe=health_probe,
    )
    result["candidate_readiness"] = {
        "ready": sorted(ready),
        "blocked": dict(sorted(blocked.items())),
    }
    _write_quality_preflight(_critical_capability_preflight_path(), result)
    return result


def main() -> None:
    # Canonical production materializes YouTube OAuth immediately before this preflight.
    # Certify the effective Google grant is read-only before any Engine process receives it.
    enforce_from_runner_temp()
    _original_main()
    # Import only after the core CLI has parsed/validated its own request. This preserves
    # Runner-only `--help` and unit-test entrypoints where the private Engine is
    # intentionally absent, while production has already installed the pinned Engine.
    from scripts.cloudflare_gold_preflight import (
        preflight_optional_gold_cloudflare_from_runner_temp,
    )

    # Cloudflare is only the fourth-choice Gold-opening Vision fallback. Probe it early
    # so a known-unavailable route is disabled before expensive production, but do not
    # turn an optional provider into a prerequisite for Planning/render/Final Master.
    preflight_optional_gold_cloudflare_from_runner_temp()

    # Release-critical capacity reservation is different: Final QC / independent Audio
    # Audit / Gold must have a deterministic eligible route *before* Planning/rendering
    # spend resources. This performs exact-model, zero-inference catalog certification
    # from the same one-time CLI key files used by the established provider preflight.
    # Independent Audio requires two distinct ready providers, so the Run #242 family is
    # rejected here instead of after Final Master.
    result = _run_critical_quality_preflight()
    print(
        "Quality Capability Preflight PASS: "
        + ", ".join(
            f"{item['capability']}={len(item['candidate_identities'])}"
            for item in result.get("reservations", [])
        )
    )


# Preserve provider_preflight's long-standing import API for all existing tests/callers.
# Imported callers receive the original implementation module with only main() hardened;
# direct execution (the production workflow path) runs the hardened main below.
_core.main = main

if __name__ == "__main__":
    main()
else:
    sys.modules[__name__] = _core
