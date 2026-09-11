from __future__ import annotations

"""One bounded, approval-shopping-safe resume of Audio Production Contract V2.

If a prior audit has one semantic review and one provider technical failure, only the
failed independent provider is retried. The semantic review is immutable evidence for
this recovery attempt; it is never re-rolled looking for a more convenient answer.
If both prior providers failed technically, both may be attempted once in normal order.
A newly confirmed second semantic review is terminal SEMANTIC_MISMATCH.
"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from scripts import audio_production_contract_v2 as contract


CONTRACT_ID = "audio.production.resume.v1"
MAX_RESUME_PROVIDER_ATTEMPTS = 2


class AudioProductionResumeError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioProductionResumeError(f"audio_resume_invalid_json:{Path(path).name}") from exc
    if not isinstance(value, dict):
        raise AudioProductionResumeError(f"audio_resume_wrong_shape:{Path(path).name}")
    return value


def _gemini_transcriber_from_api_key(api_key: str) -> Callable[[Path], str]:
    """Build a resume-only Gemini auditor from the already-consumed capability value.

    The executor captures the one-time credential once, then passes the in-memory value
    here.  The value is never restored to environment variables or persisted artifacts.
    This path is format-agnostic so AUDIO_QC_PENDING recovery works for Long and Short.
    """
    key = str(api_key or "").strip()
    if not key:
        raise AudioProductionResumeError("audio_resume_gemini_capability_missing")

    def transcribe(audio_path: Path) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise RuntimeError("google_genai_not_installed") from exc
        model = (os.environ.get("GEMINI_CONTENT_MODEL") or contract.DEFAULT_GEMINI_AUDIT_MODEL).strip()
        client = genai.Client(api_key=key)
        return contract._gemini_transcribe_with_client(
            audio_path,
            client=client,
            types_module=types,
            model=model,
        )

    return transcribe


def _transcriber_for(
    provider: str,
    *,
    groq_transcriber: Callable[[Path], str],
    gemini_transcriber: Callable[[Path], str],
) -> Callable[[Path], str]:
    if provider == "groq-whisper":
        return groq_transcriber
    if provider == "gemini-audio":
        return gemini_transcriber
    raise AudioProductionResumeError(f"audio_resume_unknown_provider:{provider}")


def _normalized_prior_attempts(prior: dict[str, Any]) -> dict[str, dict[str, Any]]:
    attempts = prior.get("attempts")
    if not isinstance(attempts, list) or not 1 <= len(attempts) <= 2:
        raise AudioProductionResumeError("audio_resume_prior_attempts_invalid")
    by_provider: dict[str, dict[str, Any]] = {}
    for item in attempts:
        if not isinstance(item, dict):
            raise AudioProductionResumeError("audio_resume_prior_attempt_invalid")
        provider = str(item.get("provider") or "").strip()
        status = str(item.get("status") or "").strip()
        if provider not in {"groq-whisper", "gemini-audio"} or status not in {"semantic_review", "technical_failure"}:
            raise AudioProductionResumeError("audio_resume_prior_outcome_not_resumable")
        if provider in by_provider:
            raise AudioProductionResumeError("audio_resume_duplicate_prior_provider")
        by_provider[provider] = dict(item)
    return by_provider


def _raise_contract_error(root: Path, document: dict[str, Any], code: contract.AudioContractErrorCode, message: str) -> None:
    exc = contract.AudioProductionContractError(code, message)
    contract._write_failure(root, document, exc)
    raise exc


def _write_pass(root: Path, document: dict[str, Any], *, accepted_provider: str) -> dict[str, Any]:
    document.update(
        {
            "decision": "pass",
            "error_code": None,
            "error": None,
            "accepted_provider": accepted_provider,
            "fallback_used": accepted_provider == "gemini-audio",
            "resume_contract_id": CONTRACT_ID,
            "resume_provider_attempts": int(document.get("resume_provider_attempts") or 0),
        }
    )
    contract._atomic_json(root / contract.AUDIT_FILENAME, document)
    print(
        "Audio Production Contract V2 resume PASS: "
        f"provider={accepted_provider} attempts={document['resume_provider_attempts']}"
    )
    return document


def resume_audio_production_contract_v2(
    output_dir: Path,
    *,
    extractor: Callable[[Path, Path], None] = contract.extract_final_audio,
    groq_transcriber: Callable[[Path], str] = contract._groq_transcribe,
    gemini_transcriber: Callable[[Path], str] = contract._gemini_transcribe,
    gemini_api_key: str | None = None,
    max_provider_attempts: int = MAX_RESUME_PROVIDER_ATTEMPTS,
) -> dict[str, Any]:
    root = Path(output_dir)
    audit_path = root / contract.AUDIT_FILENAME
    prior = _read_object(audit_path)
    if prior.get("contract_id") != contract.CONTRACT_ID or prior.get("decision") != "block":
        raise AudioProductionResumeError("audio_resume_prior_contract_not_blocked_v2")
    if prior.get("error_code") != contract.AudioContractErrorCode.AUDIT_UNAVAILABLE.value:
        raise AudioProductionResumeError("audio_resume_prior_failure_not_audit_unavailable")

    try:
        bounded_max = int(max_provider_attempts)
    except (TypeError, ValueError) as exc:
        raise AudioProductionResumeError("audio_resume_provider_budget_invalid") from exc
    if bounded_max < 0 or bounded_max > MAX_RESUME_PROVIDER_ATTEMPTS:
        raise AudioProductionResumeError("audio_resume_provider_budget_out_of_range")
    if bounded_max == 0:
        raise AudioProductionResumeError("audio_resume_provider_budget_exhausted")

    if gemini_api_key is not None:
        gemini_transcriber = _gemini_transcriber_from_api_key(gemini_api_key)

    final_path = root / "final.mp4"
    if not final_path.is_file() or final_path.stat().st_size <= 1024:
        raise AudioProductionResumeError("audio_resume_final_missing")
    final_sha = _sha256_file(final_path)
    if final_sha != str(prior.get("final_sha256") or "").strip().lower():
        raise AudioProductionResumeError("audio_resume_final_changed_since_pending_audit")

    expected = contract._expected_audio(root)
    if expected is None:
        raise AudioProductionResumeError("audio_resume_unavailable_cannot_apply_to_silent_moment")
    expected_sha = contract._sha256_text(expected.transcript)
    if expected_sha != str(prior.get("expected_transcript_sha256") or "").strip().lower():
        raise AudioProductionResumeError("audio_resume_expected_transcript_changed")
    if expected.source != str(prior.get("expected_source") or ""):
        raise AudioProductionResumeError("audio_resume_expected_source_changed")

    prior_by_provider = _normalized_prior_attempts(prior)
    semantic = [provider for provider, item in prior_by_provider.items() if item.get("status") == "semantic_review"]
    technical = [provider for provider, item in prior_by_provider.items() if item.get("status") == "technical_failure"]
    if len(semantic) >= 2:
        raise AudioProductionResumeError("audio_resume_refused_prior_confirmed_semantic_mismatch")
    if not technical:
        raise AudioProductionResumeError("audio_resume_requires_prior_technical_failure")

    # When a semantic review already exists, it is frozen and only its failed independent
    # counterpart may be retried. With no semantic evidence yet, retry each technically
    # failed provider at most once in canonical Groq -> Gemini order. The caller may
    # further reduce that count to the source video's inherited run-wide budget balance.
    if len(semantic) == 1:
        retry_order = [provider for provider in ("groq-whisper", "gemini-audio") if provider in technical]
        if len(retry_order) != 1:
            raise AudioProductionResumeError("audio_resume_independent_failed_provider_ambiguous")
    else:
        retry_order = [provider for provider in ("groq-whisper", "gemini-audio") if provider in technical]
    retry_order = retry_order[:bounded_max]
    if not retry_order:
        raise AudioProductionResumeError("audio_resume_provider_budget_exhausted")

    document = dict(prior)
    document["decision"] = "block"
    document["resume_contract_id"] = CONTRACT_ID
    document["resume_prior_audit_sha256"] = _sha256_file(audit_path)
    document["resume_policy"] = {
        "approval_shopping_forbidden": True,
        "existing_semantic_review_is_immutable": bool(semantic),
        "retry_only_prior_technical_failure": True,
        "max_provider_attempts_this_resume": bounded_max,
        "inherits_source_run_provider_budget": True,
    }
    document["resume_provider_attempts"] = 0
    latest = {provider: dict(item) for provider, item in prior_by_provider.items()}

    with tempfile.TemporaryDirectory(prefix="isco-audio-production-resume-") as temp_dir:
        audio_path = Path(temp_dir) / "final-16k-mono.flac"
        try:
            extractor(final_path, audio_path)
        except Exception as exc:
            _raise_contract_error(
                root,
                document,
                contract.AudioContractErrorCode.FINAL_ARTIFACT_INVALID,
                f"resume_audio_extract_failed:{contract._safe_error(exc)}",
            )
        if not audio_path.is_file() or audio_path.stat().st_size <= 0:
            _raise_contract_error(
                root,
                document,
                contract.AudioContractErrorCode.FINAL_ARTIFACT_INVALID,
                "resume_extracted_audio_missing",
            )
        document["extracted_audio_sha256"] = _sha256_file(audio_path)
        document["extracted_audio_bytes"] = audio_path.stat().st_size

        for provider in retry_order:
            attempt, passed = contract._provider_attempt(
                provider=provider,
                audio_path=audio_path,
                expected=expected,
                transcriber=_transcriber_for(
                    provider,
                    groq_transcriber=groq_transcriber,
                    gemini_transcriber=gemini_transcriber,
                ),
            )
            document["resume_provider_attempts"] += 1
            latest[provider] = attempt
            document["attempts"] = [
                latest[name]
                for name in ("groq-whisper", "gemini-audio")
                if name in latest
            ]
            contract._require_final_identity(final_path, final_sha)
            if passed:
                return _write_pass(root, document, accepted_provider=provider)

            semantic_now = [
                name for name, item in latest.items() if item.get("status") == "semantic_review"
            ]
            if len(semantic_now) >= 2:
                _raise_contract_error(
                    root,
                    document,
                    contract.AudioContractErrorCode.SEMANTIC_MISMATCH,
                    "spoken_audio_mismatch_confirmed_by_two_independent_auditors_after_resume",
                )

        semantic_now = [item for item in latest.values() if item.get("status") == "semantic_review"]
        technical_now = [item for item in latest.values() if item.get("status") == "technical_failure"]
        document["attempts"] = [
            latest[name]
            for name in ("groq-whisper", "gemini-audio")
            if name in latest
        ]
        if semantic_now and technical_now:
            _raise_contract_error(
                root,
                document,
                contract.AudioContractErrorCode.AUDIT_UNAVAILABLE,
                "semantic_mismatch_remains_unconfirmed_after_bounded_independent_auditor_resume",
            )
        _raise_contract_error(
            root,
            document,
            contract.AudioContractErrorCode.AUDIT_UNAVAILABLE,
            "audio_audit_providers_remain_technically_unavailable_after_bounded_resume",
        )


def require_existing_audio_production_pass(output_dir: Path) -> dict[str, Any]:
    """Zero-provider revalidation used by the normal Final Master wrapper chain."""
    root = Path(output_dir)
    document = _read_object(root / contract.AUDIT_FILENAME)
    if document.get("contract_id") != contract.CONTRACT_ID or document.get("decision") != "pass":
        raise AudioProductionResumeError("audio_resume_existing_pass_missing")
    final_path = root / "final.mp4"
    if not final_path.is_file() or _sha256_file(final_path) != str(document.get("final_sha256") or "").strip().lower():
        raise AudioProductionResumeError("audio_resume_existing_pass_final_changed")
    expected = contract._expected_audio(root)
    if expected is None:
        raise AudioProductionResumeError("audio_resume_existing_pass_expected_audio_missing")
    if contract._sha256_text(expected.transcript) != str(document.get("expected_transcript_sha256") or "").strip().lower():
        raise AudioProductionResumeError("audio_resume_existing_pass_transcript_changed")
    attempts = [item for item in list(document.get("attempts") or []) if isinstance(item, dict)]
    if not any(item.get("status") == "pass" for item in attempts):
        raise AudioProductionResumeError("audio_resume_existing_pass_has_no_passing_auditor")
    if sum(1 for item in attempts if item.get("status") == "semantic_review") >= 2:
        raise AudioProductionResumeError("audio_resume_existing_pass_contains_confirmed_mismatch")
    return document
