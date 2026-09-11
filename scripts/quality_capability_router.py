from __future__ import annotations

"""Provider-neutral routing for release-critical quality capabilities.

This module intentionally does *not* proxy provider SDKs.  It owns policy only:
capability eligibility, exact-model allowlists, credential/readiness admission,
independence, deterministic ranking, bounded retry taxonomy, provenance, and
zero-inference capacity reservation.  Existing provider adapters remain responsible
for wire transport.

Invariants:
- no provider alias/auto-router is admitted to a quality-sensitive capability;
- a valid semantic FAIL/BLOCK is terminal and is never provider-shopped;
- 429/quota moves to the next eligible candidate immediately;
- auth/config isolates the candidate for the run;
- timeout/5xx/network receives only the candidate's bounded transient retry;
- structural/schema repair is separately bounded;
- an independent auditor cannot use the provider that produced the evidence it audits;
- ranking is deterministic after eligibility/credential/health/independence filtering.
"""

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


CAP_FINAL_QC = "final_qc"
CAP_AUDIO_SEMANTIC_AUDIT = "audio_semantic_audit"
CAP_INDEPENDENT_AUDIO_AUDIT = "independent_audio_audit"
CAP_GOLD_VISION = "gold_vision"
CAP_GOLD_TEXT = "gold_text"

TIER_PRODUCTION_FREE = "production_free"
TIER_EMERGENCY = "emergency"

# Never admit provider-owned dynamic aliases in release-critical quality paths.
_FORBIDDEN_MODEL_IDS = frozenset(
    {
        "openrouter/free",
        "openrouter/auto",
        "auto",
        "latest",
        "mistral-small-latest",
    }
)


@dataclass(frozen=True, slots=True)
class CapabilityCandidate:
    provider: str
    model: str
    tier: str
    priority: int
    quota_domain: str
    max_transient_retries: int = 1
    max_schema_repairs: int = 1

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    capability: str
    candidates: tuple[CapabilityCandidate, ...]
    require_independent_provider: bool = False
    min_distinct_ready_providers: int = 1
    fail_closed: bool = True


# Exact IDs only.  OpenRouter entries are explicit model slugs that were already used
# by this repository historically; the dynamic `openrouter/free` router is forbidden.
_CAPABILITY_REGISTRY: dict[str, CapabilityPolicy] = {
    CAP_FINAL_QC: CapabilityPolicy(
        capability=CAP_FINAL_QC,
        candidates=(
            CapabilityCandidate("groq", "whisper-large-v3-turbo", TIER_PRODUCTION_FREE, 10, "audio_transcription"),
            CapabilityCandidate("gemini", "gemini-3.7-flash", TIER_PRODUCTION_FREE, 20, "audio_understanding"),
        ),
        min_distinct_ready_providers=2,
    ),
    CAP_AUDIO_SEMANTIC_AUDIT: CapabilityPolicy(
        capability=CAP_AUDIO_SEMANTIC_AUDIT,
        candidates=(
            CapabilityCandidate("groq", "whisper-large-v3-turbo", TIER_PRODUCTION_FREE, 10, "audio_transcription"),
            CapabilityCandidate("gemini", "gemini-3.7-flash", TIER_PRODUCTION_FREE, 20, "audio_understanding"),
        ),
        min_distinct_ready_providers=2,
    ),
    CAP_INDEPENDENT_AUDIO_AUDIT: CapabilityPolicy(
        capability=CAP_INDEPENDENT_AUDIO_AUDIT,
        candidates=(
            CapabilityCandidate("groq", "whisper-large-v3-turbo", TIER_PRODUCTION_FREE, 10, "audio_transcription"),
            CapabilityCandidate("gemini", "gemini-3.7-flash", TIER_PRODUCTION_FREE, 20, "audio_understanding"),
        ),
        require_independent_provider=True,
        min_distinct_ready_providers=2,
    ),
    CAP_GOLD_VISION: CapabilityPolicy(
        capability=CAP_GOLD_VISION,
        candidates=(
            CapabilityCandidate("gemini", "gemini-3.7-flash", TIER_PRODUCTION_FREE, 10, "generate_content"),
            CapabilityCandidate("groq", "qwen/qwen3.8-27b", TIER_PRODUCTION_FREE, 20, "vision"),
            CapabilityCandidate("openrouter", "google/gemma-4-26b-a4b-it:free", TIER_EMERGENCY, 100, "vision"),
            CapabilityCandidate("openrouter", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free", TIER_EMERGENCY, 110, "vision"),
        ),
    ),
    CAP_GOLD_TEXT: CapabilityPolicy(
        capability=CAP_GOLD_TEXT,
        candidates=(
            CapabilityCandidate("gemini", "gemini-3.7-flash", TIER_PRODUCTION_FREE, 10, "generate_content"),
            CapabilityCandidate("openrouter", "openai/gpt-oss-20b:free", TIER_EMERGENCY, 100, "text"),
        ),
    ),
}


class CapabilityRouteError(RuntimeError):
    pass


class RetryAction(str, Enum):
    ACCEPT = "accept"
    TERMINAL_QUALITY_FAIL = "terminal_quality_fail"
    FALLBACK = "fallback"
    ISOLATE_AND_FALLBACK = "isolate_and_fallback"
    RETRY_SAME = "retry_same"
    REPAIR_SAME = "repair_same"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class ArtifactProvenance:
    artifact: str
    capability: str
    provider: str
    model: str
    artifact_sha256: str | None = None
    subject_sha256: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    identity: str
    reason: str


@dataclass(frozen=True, slots=True)
class RouteDecision:
    capability: str
    candidates: tuple[CapabilityCandidate, ...]
    rejected: tuple[RejectedCandidate, ...]


@dataclass(frozen=True, slots=True)
class CapabilityReservation:
    capability: str
    candidate_identities: tuple[str, ...]
    distinct_providers: tuple[str, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RetryDecision:
    action: RetryAction
    reason: str


CredentialProbe = Callable[[CapabilityCandidate], bool]
HealthProbe = Callable[[CapabilityCandidate], object | None]


def capability_registry() -> Mapping[str, CapabilityPolicy]:
    return dict(_CAPABILITY_REGISTRY)


def policy_for(capability: str) -> CapabilityPolicy:
    try:
        return _CAPABILITY_REGISTRY[str(capability)]
    except KeyError as exc:
        raise CapabilityRouteError(f"unknown_quality_capability:{capability}") from exc


def _validate_registry() -> None:
    for capability, policy in _CAPABILITY_REGISTRY.items():
        if policy.capability != capability or not policy.candidates:
            raise CapabilityRouteError(f"invalid_capability_policy:{capability}")
        seen: set[str] = set()
        for candidate in policy.candidates:
            provider = candidate.provider.strip().lower()
            model = candidate.model.strip()
            if provider not in {"gemini", "groq", "openrouter"}:
                raise CapabilityRouteError(f"unsupported_quality_provider:{provider}")
            if not model or model.casefold() in _FORBIDDEN_MODEL_IDS:
                raise CapabilityRouteError(
                    f"forbidden_or_aliased_model capability={capability} provider={provider} model={model}"
                )
            if model.endswith("-latest") or model.endswith(":auto"):
                raise CapabilityRouteError(
                    f"forbidden_model_alias capability={capability} provider={provider} model={model}"
                )
            if candidate.tier not in {TIER_PRODUCTION_FREE, TIER_EMERGENCY}:
                raise CapabilityRouteError(f"invalid_provider_tier:{candidate.tier}")
            if candidate.identity in seen:
                raise CapabilityRouteError(f"duplicate_capability_candidate:{candidate.identity}")
            seen.add(candidate.identity)


_validate_registry()


def _secret_present(name: str) -> bool:
    if str(os.environ.get(name) or "").strip():
        return True
    file_name = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not file_name:
        return False
    try:
        path = Path(file_name)
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def default_credential_probe(candidate: CapabilityCandidate) -> bool:
    env_name = {
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }[candidate.provider]
    return _secret_present(env_name)


def default_health_probe(candidate: CapabilityCandidate) -> object | None:
    try:
        from scripts import provider_health_registry as health

        health.load_preflight_provider_health()
        return health.provider_unavailable(
            candidate.provider,
            model=candidate.model,
            quota_domain=candidate.quota_domain,
        )
    except ImportError:
        return None


def route_candidates(
    capability: str,
    *,
    credential_probe: CredentialProbe = default_credential_probe,
    health_probe: HealthProbe = default_health_probe,
    exclude_provenance: Sequence[ArtifactProvenance] = (),
    isolated_identities: Iterable[str] = (),
) -> RouteDecision:
    """Apply the complete decision sequence and return a deterministic ranked route."""
    policy = policy_for(capability)
    isolated = {str(value).strip() for value in isolated_identities if str(value).strip()}
    excluded_providers = {
        item.provider.strip().lower()
        for item in exclude_provenance
        if policy.require_independent_provider
    }
    rejected: list[RejectedCandidate] = []
    admitted: list[CapabilityCandidate] = []

    # eligibility -> credentials -> quota/health -> independence -> ranked candidates
    for candidate in policy.candidates:
        if candidate.identity in isolated:
            rejected.append(RejectedCandidate(candidate.identity, "isolated_after_runtime_failure"))
            continue
        if not credential_probe(candidate):
            rejected.append(RejectedCandidate(candidate.identity, "credential_unavailable"))
            continue
        health_evidence = health_probe(candidate)
        if health_evidence is not None:
            reason = str(getattr(health_evidence, "reason", "provider_unavailable"))[:180]
            rejected.append(RejectedCandidate(candidate.identity, f"health_block:{reason}"))
            continue
        if candidate.provider in excluded_providers:
            rejected.append(RejectedCandidate(candidate.identity, "independence_provider_conflict"))
            continue
        admitted.append(candidate)

    tier_rank = {TIER_PRODUCTION_FREE: 0, TIER_EMERGENCY: 1}
    admitted.sort(key=lambda item: (tier_rank[item.tier], item.priority, item.provider, item.model))
    return RouteDecision(capability, tuple(admitted), tuple(rejected))


def require_route(
    capability: str,
    **kwargs,
) -> RouteDecision:
    decision = route_candidates(capability, **kwargs)
    policy = policy_for(capability)
    distinct = {item.provider for item in decision.candidates}
    if len(distinct) < policy.min_distinct_ready_providers:
        reasons = ";".join(f"{item.identity}={item.reason}" for item in decision.rejected)
        raise CapabilityRouteError(
            f"CAPABILITY_UNAVAILABLE capability={capability} "
            f"ready_providers={len(distinct)}/{policy.min_distinct_ready_providers} {reasons}"
        )
    return decision


def classify_retry(
    *,
    candidate: CapabilityCandidate,
    error: BaseException | str | None = None,
    http_status: int | None = None,
    valid_result: bool = False,
    semantic_pass: bool | None = None,
    transient_retry_count: int = 0,
    schema_repair_count: int = 0,
) -> RetryDecision:
    """Classify one attempt without confusing quality verdicts with provider failures."""
    if valid_result:
        if semantic_pass is False:
            return RetryDecision(RetryAction.TERMINAL_QUALITY_FAIL, "valid_semantic_fail_is_authoritative")
        return RetryDecision(RetryAction.ACCEPT, "valid_result")

    detail = str(error or "").casefold()
    status = http_status
    if status is None:
        for code in (401, 403, 408, 429, 500, 502, 503, 504):
            if str(code) in detail:
                status = code
                break

    if status == 429 or any(token in detail for token in ("rate limit", "rate_limit", "quota", "resource_exhausted")):
        return RetryDecision(RetryAction.FALLBACK, "rate_or_quota_failure")
    if status in {401, 403} or any(
        token in detail
        for token in ("unauthorized", "forbidden", "authentication", "invalid api key", "api_key_missing")
    ):
        return RetryDecision(RetryAction.ISOLATE_AND_FALLBACK, "auth_or_credential_failure")
    if (
        status in {408, 500, 502, 503, 504}
        or any(token in detail for token in ("timeout", "timed out", "network", "connection", "service unavailable"))
    ):
        if transient_retry_count < candidate.max_transient_retries:
            return RetryDecision(RetryAction.RETRY_SAME, "bounded_transient_retry")
        return RetryDecision(RetryAction.FALLBACK, "transient_retry_exhausted")
    if any(
        token in detail
        for token in ("invalid json", "schema", "structural", "json_validate_failed", "response has no choice")
    ):
        if schema_repair_count < candidate.max_schema_repairs:
            return RetryDecision(RetryAction.REPAIR_SAME, "bounded_schema_repair")
        return RetryDecision(RetryAction.FALLBACK, "schema_repair_exhausted")
    return RetryDecision(RetryAction.FALLBACK, "technical_failure")


def reserve_capability(
    capability: str,
    **kwargs,
) -> CapabilityReservation:
    decision = require_route(capability, **kwargs)
    providers = tuple(dict.fromkeys(item.provider for item in decision.candidates))
    canonical = json.dumps(
        {
            "capability": capability,
            "candidates": [item.identity for item in decision.candidates],
            "providers": list(providers),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CapabilityReservation(
        capability=capability,
        candidate_identities=tuple(item.identity for item in decision.candidates),
        distinct_providers=providers,
        fingerprint=hashlib.sha256(canonical).hexdigest(),
    )


def critical_capacity_preflight(
    *,
    credential_probe: CredentialProbe = default_credential_probe,
    health_probe: HealthProbe = default_health_probe,
) -> dict:
    """Reserve zero-inference routes before expensive planning/render work starts."""
    capabilities = (
        CAP_FINAL_QC,
        CAP_INDEPENDENT_AUDIO_AUDIT,
        CAP_GOLD_VISION,
        CAP_GOLD_TEXT,
    )
    reservations = [
        reserve_capability(
            capability,
            credential_probe=credential_probe,
            health_probe=health_probe,
        )
        for capability in capabilities
    ]
    return {
        "schema_version": 1,
        "contract_id": "quality.capability-preflight.v1",
        "decision": "pass",
        "reservations": [asdict(item) for item in reservations],
    }


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def write_critical_capacity_preflight(path: Path) -> dict:
    payload = critical_capacity_preflight()
    _atomic_json(Path(path), payload)
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def provenance_sidecar(artifact: Path) -> Path:
    artifact = Path(artifact)
    return artifact.with_name(artifact.name + ".provider-provenance.json")


def record_artifact_provenance(
    artifact: Path,
    *,
    capability: str,
    provider: str,
    model: str,
    subject: Path | None = None,
) -> ArtifactProvenance:
    artifact = Path(artifact)
    if not artifact.is_file():
        raise CapabilityRouteError(f"provenance_artifact_missing:{artifact}")
    item = ArtifactProvenance(
        artifact=str(artifact.name),
        capability=str(capability),
        provider=str(provider).strip().lower(),
        model=str(model).strip(),
        artifact_sha256=_sha256_file(artifact),
        subject_sha256=_sha256_file(subject) if subject is not None and Path(subject).is_file() else None,
    )
    _atomic_json(provenance_sidecar(artifact), {"schema_version": 1, **asdict(item)})
    return item


def read_artifact_provenance(artifact: Path) -> ArtifactProvenance | None:
    path = provenance_sidecar(Path(artifact))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return ArtifactProvenance(
            artifact=str(payload["artifact"]),
            capability=str(payload["capability"]),
            provider=str(payload["provider"]).strip().lower(),
            model=str(payload["model"]),
            artifact_sha256=str(payload.get("artifact_sha256") or "") or None,
            subject_sha256=str(payload.get("subject_sha256") or "") or None,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return None
