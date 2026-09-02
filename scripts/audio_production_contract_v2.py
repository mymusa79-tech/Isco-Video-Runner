from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from scripts.groq_audio_audit import (
    DEFAULT_AUDIO_MODEL,
    GroqRateLimited,
    compare_transcripts,
    extract_final_audio,
    narration_from_plan,
    transcribe_audio,
)

CONTRACT_ID = "audio.production.v2"
SCHEMA_VERSION = 2
AUDIT_FILENAME = "audio-production-contract-v2.json"
DEFAULT_GEMINI_AUDIT_MODEL = "gemini-3.7-flash"
MAX_PROVIDER_ATTEMPTS = 2
MAX_INLINE_AUDIO_BYTES = 20 * 1024 * 1024


class AudioContractErrorCode(str, Enum):
    FINAL_ARTIFACT_INVALID = "FINAL_ARTIFACT_INVALID"
    EXPECTED_TRANSCRIPT_INVALID = "EXPECTED_TRANSCRIPT_INVALID"
    PROVIDER_CAPACITY = "PROVIDER_CAPACITY"
    PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
    PROVIDER_AUTH = "PROVIDER_AUTH"
    STRUCTURAL_INVALID = "STRUCTURAL_INVALID"
    SEMANTIC_MISMATCH = "SEMANTIC_MISMATCH"
    AUDIT_UNAVAILABLE = "AUDIT_UNAVAILABLE"
    INTERNAL_CONTRACT_ERROR = "INTERNAL_CONTRACT_ERROR"


class AudioProductionContractError(RuntimeError):
    def __init__(self, code: AudioContractErrorCode, message: str):
        super().__init__(f"{code.value}:{message}")
        self.code = code


@dataclass(frozen=True, slots=True)
class ExpectedAudio:
    scope: str
    transcript: str
    source: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(str(text).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AudioProductionContractError(
            AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
            f"invalid_json:{path.name}",
        ) from exc
    if not isinstance(value, dict):
        raise AudioProductionContractError(
            AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
            f"wrong_shape:{path.name}",
        )
    return value


def _secret_from_env(name: str) -> str:
    direct = (os.environ.get(name) or "").strip()
    if direct:
        return direct
    file_name = (os.environ.get(f"{name}_FILE") or "").strip()
    if not file_name:
        return ""
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def _expected_audio(output_dir: Path) -> ExpectedAudio | None:
    root = Path(output_dir)
    short_path = root / "short-intelligence-pre-gold.json"
    if short_path.is_file():
        short_doc = _read_json(short_path)
        voice = short_doc.get("voice")
        if not isinstance(voice, dict):
            raise AudioProductionContractError(
                AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
                "short_voice_contract_missing",
            )
        transcript = str(voice.get("transcript") or "").strip()
        if not transcript:
            raise AudioProductionContractError(
                AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
                "short_voice_transcript_missing",
            )
        return ExpectedAudio(scope="short", transcript=transcript, source=short_path.name)

    plan_path = root / "plan.json"
    plan = _read_json(plan_path)
    fmt = str(plan.get("format") or "").strip().lower()
    if fmt == "moment":
        return None
    try:
        transcript = narration_from_plan(plan_path).strip()
    except Exception as exc:
        raise AudioProductionContractError(
            AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
            "long_plan_narration_missing",
        ) from exc
    if not transcript:
        raise AudioProductionContractError(
            AudioContractErrorCode.EXPECTED_TRANSCRIPT_INVALID,
            "long_plan_narration_empty",
        )
    return ExpectedAudio(scope="long", transcript=transcript, source=plan_path.name)


def _classify_provider_failure(exc: Exception) -> AudioContractErrorCode:
    if isinstance(exc, GroqRateLimited):
        return AudioContractErrorCode.PROVIDER_CAPACITY
    text = f"{type(exc).__name__}:{exc}".lower()
    if any(token in text for token in ("429", "rate limit", "rate_limited", "resource_exhausted", "quota")):
        return AudioContractErrorCode.PROVIDER_CAPACITY
    if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "api key", "api_key")):
        return AudioContractErrorCode.PROVIDER_AUTH
    if any(token in text for token in ("timeout", "timed out", "network", "503", "502", "500", "unavailable")):
        return AudioContractErrorCode.PROVIDER_TRANSIENT
    return AudioContractErrorCode.STRUCTURAL_INVALID


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}:{str(exc)[:200]}"


def _groq_transcribe(audio_path: Path) -> str:
    key = _secret_from_env("GROQ_API_KEY")
    if not key:
        raise RuntimeError("groq_api_key_missing")
    text, _telemetry = transcribe_audio(
        audio_path,
        api_key=key,
        model=DEFAULT_AUDIO_MODEL,
    )
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("groq_transcription_empty")
    return text.strip()


def _gemini_transcribe(audio_path: Path) -> str:
    key = _secret_from_env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("gemini_api_key_missing")
    data = audio_path.read_bytes()
    if not data:
        raise RuntimeError("gemini_audio_input_empty")
    if len(data) > MAX_INLINE_AUDIO_BYTES:
        raise RuntimeError(f"gemini_inline_audio_too_large:{len(data)}")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise RuntimeError("google_genai_not_installed") from exc

    model = (os.environ.get("GEMINI_CONTENT_MODEL") or DEFAULT_GEMINI_AUDIT_MODEL).strip()
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=model,
        contents=[
            (
                "Transcribe the spoken Arabic exactly as heard. Return transcript text only. "
                "Do not summarize, correct, translate, explain, or infer missing words."
            ),
            types.Part.from_bytes(data=data, mime_type="audio/flac"),
        ],
    )
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("gemini_transcription_empty")
    return text.strip()


def _provider_attempt(
    *,
    provider: str,
    audio_path: Path,
    expected: ExpectedAudio,
    transcriber: Callable[[Path], str],
) -> tuple[dict[str, Any], bool]:
    try:
        actual = transcriber(audio_path)
        comparison = compare_transcripts(expected.transcript, actual)
        passed = comparison.get("decision") == "pass"
        return (
            {
                "provider": provider,
                "status": "pass" if passed else "semantic_review",
                "transcript_sha256": _sha256_text(actual),
                "comparison": comparison,
            },
            passed,
        )
    except Exception as exc:
        return (
            {
                "provider": provider,
                "status": "technical_failure",
                "error_code": _classify_provider_failure(exc).value,
                "error": _safe_error(exc),
            },
            False,
        )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _write_failure(root: Path, document: dict[str, Any], exc: Exception) -> None:
    document["decision"] = "block"
    if isinstance(exc, AudioProductionContractError):
        document["error_code"] = exc.code.value
    else:
        document["error_code"] = AudioContractErrorCode.INTERNAL_CONTRACT_ERROR.value
    document["error"] = _safe_error(exc)
    _atomic_json(root / AUDIT_FILENAME, document)


def require_audio_production_contract_v2(
    output_dir: Path,
    *,
    extractor: Callable[[Path, Path], None] = extract_final_audio,
    groq_transcriber: Callable[[Path], str] = _groq_transcribe,
    gemini_transcriber: Callable[[Path], str] = _gemini_transcribe,
) -> dict[str, Any]:
    """Enforce spoken-audio fidelity on the exact final artifact.

    The contract is shared by Long and Short. It performs no blind retry:
    at most one Groq ASR attempt and one Gemini audio-understanding fallback.
    A primary semantic mismatch is confirmed by the independent fallback before
    blocking; technical exhaustion is fail-closed.
    """
    root = Path(output_dir)
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract_id": CONTRACT_ID,
        "decision": "block",
        "max_provider_attempts": MAX_PROVIDER_ATTEMPTS,
        "attempts": [],
    }
    try:
        final_path = root / "final.mp4"
        if not final_path.is_file() or final_path.stat().st_size <= 1024:
            raise AudioProductionContractError(
                AudioContractErrorCode.FINAL_ARTIFACT_INVALID,
                "final_mp4_missing_or_too_small",
            )

        expected = _expected_audio(root)
        document["final_sha256"] = _sha256_file(final_path)
        if expected is None:
            document.update(
                {
                    "decision": "not_applicable",
                    "scope": "silent_moment",
                    "reason": "moment format has no finished Short voice contract",
                }
            )
            _atomic_json(root / AUDIT_FILENAME, document)
            return document

        document.update(
            {
                "scope": expected.scope,
                "expected_source": expected.source,
                "expected_transcript_sha256": _sha256_text(expected.transcript),
                "expected_transcript_utf8_bytes": len(expected.transcript.encode("utf-8")),
            }
        )

        with tempfile.TemporaryDirectory(prefix="isco-audio-contract-v2-") as temp_dir:
            audio_path = Path(temp_dir) / "final-16k-mono.flac"
            try:
                extractor(final_path, audio_path)
            except Exception as exc:
                raise AudioProductionContractError(
                    AudioContractErrorCode.FINAL_ARTIFACT_INVALID,
                    f"audio_extract_failed:{_safe_error(exc)}",
                ) from exc
            if not audio_path.is_file() or audio_path.stat().st_size <= 0:
                raise AudioProductionContractError(
                    AudioContractErrorCode.FINAL_ARTIFACT_INVALID,
                    "extracted_audio_missing",
                )
            document["extracted_audio_sha256"] = _sha256_file(audio_path)
            document["extracted_audio_bytes"] = audio_path.stat().st_size

            groq_attempt, groq_pass = _provider_attempt(
                provider="groq-whisper",
                audio_path=audio_path,
                expected=expected,
                transcriber=groq_transcriber,
            )
            document["attempts"].append(groq_attempt)
            if groq_pass:
                document.update(
                    {
                        "decision": "pass",
                        "accepted_provider": "groq-whisper",
                        "fallback_used": False,
                    }
                )
                _atomic_json(root / AUDIT_FILENAME, document)
                print(f"Audio Production Contract V2 PASS: scope={expected.scope} provider=groq-whisper")
                return document

            gemini_attempt, gemini_pass = _provider_attempt(
                provider="gemini-audio",
                audio_path=audio_path,
                expected=expected,
                transcriber=gemini_transcriber,
            )
            document["attempts"].append(gemini_attempt)
            if gemini_pass:
                document.update(
                    {
                        "decision": "pass",
                        "accepted_provider": "gemini-audio",
                        "fallback_used": True,
                        "primary_outcome": groq_attempt.get("status"),
                    }
                )
                _atomic_json(root / AUDIT_FILENAME, document)
                print(f"Audio Production Contract V2 PASS: scope={expected.scope} provider=gemini-audio fallback")
                return document

            semantic_reviews = [
                item for item in document["attempts"]
                if item.get("status") == "semantic_review"
            ]
            if semantic_reviews:
                raise AudioProductionContractError(
                    AudioContractErrorCode.SEMANTIC_MISMATCH,
                    "spoken_audio_does_not_match_expected_transcript",
                )
            raise AudioProductionContractError(
                AudioContractErrorCode.AUDIT_UNAVAILABLE,
                "both_audio_audit_providers_failed_technically",
            )

    except Exception as exc:
        _write_failure(root, document, exc)
        if isinstance(exc, AudioProductionContractError):
            raise
        raise AudioProductionContractError(
            AudioContractErrorCode.INTERNAL_CONTRACT_ERROR,
            _safe_error(exc),
        ) from exc
