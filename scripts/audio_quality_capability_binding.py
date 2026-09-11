from __future__ import annotations

"""Capability-routed binding for Audio Production Contract V2.

The V2 contract remains the semantic authority and still owns the exact transcript
comparison/thresholds/fail-closed verdict.  This binding changes only provider policy:
exact model admission, health/credential checks, bounded technical retry, independent
fallback enforcement, and accepted-audit provenance.
"""

from pathlib import Path
from typing import Callable

from scripts import audio_production_contract_v2 as contract
from scripts import quality_capability_router as router


_GROQ_PROVIDER = "groq"
_GEMINI_PROVIDER = "gemini"


def _candidate(provider: str) -> router.CapabilityCandidate:
    matches = [
        item
        for item in router.policy_for(router.CAP_AUDIO_SEMANTIC_AUDIT).candidates
        if item.provider == provider
    ]
    if len(matches) != 1:
        raise router.CapabilityRouteError(
            f"audio_capability_candidate_not_unique provider={provider} count={len(matches)}"
        )
    return matches[0]


def _credential_probe(candidate: router.CapabilityCandidate) -> bool:
    if candidate.provider == _GROQ_PROVIDER:
        return bool(contract._secret_from_env("GROQ_API_KEY"))
    if candidate.provider == _GEMINI_PROVIDER:
        try:
            return bool(contract._resolve_gemini_audit_key())
        except Exception:
            return False
    return False


def _admitted(
    capability: str,
    candidate: router.CapabilityCandidate,
    *,
    exclude_provenance: tuple[router.ArtifactProvenance, ...] = (),
) -> bool:
    decision = router.route_candidates(
        capability,
        credential_probe=_credential_probe,
        exclude_provenance=exclude_provenance,
    )
    return any(item.identity == candidate.identity for item in decision.candidates)


def _bounded_transcriber(
    candidate: router.CapabilityCandidate,
    transcriber: Callable[[Path], str],
    *,
    capability: str,
    exclude_provenance: tuple[router.ArtifactProvenance, ...] = (),
) -> Callable[[Path], str]:
    """Apply retry taxonomy around one existing provider adapter, never semantics."""

    def call(audio_path: Path) -> str:
        if not _admitted(
            capability,
            candidate,
            exclude_provenance=exclude_provenance,
        ):
            raise RuntimeError(
                f"capability_route_unavailable:{capability}:{candidate.identity}"
            )

        transient_retries = 0
        schema_repairs = 0
        while True:
            try:
                return transcriber(Path(audio_path))
            except Exception as exc:
                decision = router.classify_retry(
                    candidate=candidate,
                    error=exc,
                    transient_retry_count=transient_retries,
                    schema_repair_count=schema_repairs,
                )
                if decision.action is router.RetryAction.RETRY_SAME:
                    transient_retries += 1
                    continue
                # Audio transcription has no separate prompt/JSON repair transport.
                # A structural retry is therefore counted against the one bounded
                # repair slot but reuses the same exact adapter call once only.
                if decision.action is router.RetryAction.REPAIR_SAME:
                    schema_repairs += 1
                    continue
                raise

    return call


def require_audio_production_contract_v2_routed(
    output_dir: Path,
    *,
    extractor=contract.extract_final_audio,
    groq_transcriber: Callable[[Path], str] = contract._groq_transcribe,
    gemini_transcriber: Callable[[Path], str] = contract._gemini_transcribe,
) -> dict:
    """Run V2 with provider-neutral policy while preserving its quality semantics."""
    root = Path(output_dir)
    groq_candidate = _candidate(_GROQ_PROVIDER)
    gemini_candidate = _candidate(_GEMINI_PROVIDER)

    primary_provenance = router.ArtifactProvenance(
        artifact="audio-primary-audit-attempt",
        capability=router.CAP_AUDIO_SEMANTIC_AUDIT,
        provider=groq_candidate.provider,
        model=groq_candidate.model,
    )

    result = contract.require_audio_production_contract_v2(
        root,
        extractor=extractor,
        groq_transcriber=_bounded_transcriber(
            groq_candidate,
            groq_transcriber,
            capability=router.CAP_AUDIO_SEMANTIC_AUDIT,
        ),
        gemini_transcriber=_bounded_transcriber(
            gemini_candidate,
            gemini_transcriber,
            capability=router.CAP_INDEPENDENT_AUDIO_AUDIT,
            exclude_provenance=(primary_provenance,),
        ),
    )

    if result.get("decision") == "pass":
        accepted = str(result.get("accepted_provider") or "").strip()
        if accepted == "groq-whisper":
            candidate = groq_candidate
        elif accepted == "gemini-audio":
            candidate = gemini_candidate
        else:
            raise router.CapabilityRouteError(
                f"audio_contract_unknown_accepted_provider:{accepted or 'missing'}"
            )
        audit_path = root / contract.AUDIT_FILENAME
        final_path = root / "final.mp4"
        router.record_artifact_provenance(
            audit_path,
            capability=router.CAP_AUDIO_SEMANTIC_AUDIT,
            provider=candidate.provider,
            model=candidate.model,
            subject=final_path,
        )
    return result
