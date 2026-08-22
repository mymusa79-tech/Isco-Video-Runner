from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unicodedata
import urllib.error
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

GROQ_TRANSCRIPTION_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_AUDIO_MODEL = "whisper-large-v3-turbo"
AUDIT_FILENAME = "audio-transcript-audit.json"
MODE = "observe_only"

# G1: explicit free-tier model policy. Unknown models are denied; there is no
# automatic paid fallback or model upgrade path in this module.
FREE_ONLY_MODELS = frozenset(
    {
        "openai/gpt-oss-20b",
        "openai/gpt-oss-120b",
        "whisper-large-v3-turbo",
        "qwen/qwen3.6-27b",
    }
)

PASS_TOKEN_RECALL = 0.93
PASS_CHAR_SIMILARITY = 0.90

_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
_NON_WORD = re.compile(r"[^\w\s\u0600-\u06FF]+", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


class GroqFreeOnlyViolation(RuntimeError):
    pass


class GroqRateLimited(RuntimeError):
    pass


class GroqAudioAuditError(RuntimeError):
    pass


def assert_free_only_model(model: str) -> str:
    model = (model or "").strip()
    if model not in FREE_ONLY_MODELS:
        raise GroqFreeOnlyViolation(f"groq_model_not_free_only:{model or '<empty>'}")
    return model


def _governor_telemetry(model: str, *, status: str, http_status: int | None = None) -> dict[str, Any]:
    return {
        "policy": "free_only",
        "model": model,
        "allowed": model in FREE_ONLY_MODELS,
        "status": status,
        "http_status": http_status,
        "auto_upgrade_enabled": False,
        "paid_fallback_enabled": False,
        "rate_limit_action": "skip_audit_no_paid_fallback" if http_status == 429 else None,
    }


def normalize_arabic(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("ـ", "")
    text = _ARABIC_DIACRITICS.sub("", text)
    text = text.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ٱ": "ا",
                "ى": "ي",
                "ؤ": "و",
                "ئ": "ي",
            }
        )
    )
    text = _NON_WORD.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def _matching_token_count(expected_tokens: list[str], actual_tokens: list[str]) -> int:
    matcher = SequenceMatcher(a=expected_tokens, b=actual_tokens, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks())


def compare_transcripts(expected: str, actual: str) -> dict[str, Any]:
    expected_normalized = normalize_arabic(expected)
    actual_normalized = normalize_arabic(actual)
    expected_tokens = expected_normalized.split()
    actual_tokens = actual_normalized.split()
    if not expected_tokens:
        raise GroqAudioAuditError("expected_narration_empty")

    matched = _matching_token_count(expected_tokens, actual_tokens)
    recall = matched / len(expected_tokens)
    precision = matched / len(actual_tokens) if actual_tokens else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    char_similarity = SequenceMatcher(
        a=expected_normalized,
        b=actual_normalized,
        autojunk=False,
    ).ratio()
    length_ratio = len(actual_tokens) / len(expected_tokens)

    decision = (
        "pass"
        if recall >= PASS_TOKEN_RECALL and char_similarity >= PASS_CHAR_SIMILARITY
        else "review"
    )
    return {
        "decision": decision,
        "expected_tokens": len(expected_tokens),
        "transcribed_tokens": len(actual_tokens),
        "matched_tokens_lcs": matched,
        "token_recall": round(recall, 6),
        "token_precision": round(precision, 6),
        "token_f1": round(f1, 6),
        "char_similarity": round(char_similarity, 6),
        "length_ratio": round(length_ratio, 6),
        "thresholds": {
            "token_recall": PASS_TOKEN_RECALL,
            "char_similarity": PASS_CHAR_SIMILARITY,
        },
    }


def _sections_from_plan(data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [data.get("sections")]
    for key in ("script", "plan"):
        nested = data.get(key)
        if isinstance(nested, dict):
            candidates.append(nested.get("sections"))
    for candidate in candidates:
        if isinstance(candidate, list):
            sections = [item for item in candidate if isinstance(item, dict)]
            if sections:
                return sections
    return []


def narration_from_plan(plan_path: Path) -> str:
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GroqAudioAuditError("plan_json_not_object")
    sections = _sections_from_plan(data)
    narration = "\n".join(
        str(section.get("narration", "")).strip()
        for section in sections
        if str(section.get("narration", "")).strip()
    ).strip()
    if not narration:
        direct = data.get("narration")
        narration = str(direct).strip() if direct else ""
    if not narration:
        raise GroqAudioAuditError("plan_narration_missing")
    return narration


def extract_final_audio(final_video: Path, audio_path: Path) -> None:
    if not final_video.is_file():
        raise GroqAudioAuditError("final_video_missing")
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(final_video),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "flac",
        str(audio_path),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise GroqAudioAuditError("ffmpeg_not_found") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().replace("\n", " ")[:240]
        raise GroqAudioAuditError(f"ffmpeg_audio_extract_failed:{detail}") from exc
    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise GroqAudioAuditError("extracted_audio_empty")


def _multipart_body(audio_path: Path, *, model: str) -> tuple[bytes, str]:
    boundary = "----isco-groq-audio-audit-boundary"
    audio = audio_path.read_bytes()
    parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nar\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"temperature\"\r\n\r\n0\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode(),
        (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{audio_path.name}\"\r\n"
            "Content-Type: audio/flac\r\n\r\n"
        ).encode(),
        audio,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), boundary


def transcribe_audio(
    audio_path: Path,
    *,
    api_key: str,
    model: str = DEFAULT_AUDIO_MODEL,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[str, dict[str, Any]]:
    model = assert_free_only_model(model)
    if model != DEFAULT_AUDIO_MODEL:
        raise GroqFreeOnlyViolation(f"groq_audio_model_not_approved:{model}")
    if not (api_key or "").strip():
        raise GroqAudioAuditError("groq_api_key_missing")

    body, boundary = _multipart_body(audio_path, model=model)
    request = urllib.request.Request(
        GROQ_TRANSCRIPTION_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
            "User-Agent": "isco-video-runner-groq-audio-audit/1",
        },
    )
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
            http_status = int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise GroqRateLimited("groq_audio_rate_limited_429") from exc
        detail = exc.read().decode("utf-8", errors="replace")[:240]
        raise GroqAudioAuditError(f"groq_http_{exc.code}:{detail}") from exc
    except urllib.error.URLError as exc:
        raise GroqAudioAuditError(f"groq_network_error:{exc.reason}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise GroqAudioAuditError("groq_transcription_response_invalid")
    return payload["text"].strip(), _governor_telemetry(model, status="ok", http_status=http_status)


def _write_audit(path: Path, document: dict[str, Any]) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def run_groq_audio_audit(
    output_dir: Path,
    *,
    api_key: str | None,
    model: str = DEFAULT_AUDIO_MODEL,
    transcriber: Callable[..., tuple[str, dict[str, Any]]] = transcribe_audio,
    extractor: Callable[[Path, Path], None] = extract_final_audio,
) -> dict[str, Any]:
    """G1+G2 observer. It never raises into production and never changes the video.

    G1 enforces the Groq free-only model policy and records telemetry. G2 extracts
    final.mp4 audio, transcribes it with Whisper, compares it with plan narration,
    and writes audio-transcript-audit.json. Any failure becomes audit_error/rate_limited.
    """
    output_dir = Path(output_dir)
    audit_path = output_dir / AUDIT_FILENAME
    document: dict[str, Any] = {
        "schema_version": 1,
        "mode": MODE,
        "enforcement": "disabled",
        "final_video": "final.mp4",
        "plan": "plan.json",
        "groq_governor": _governor_telemetry(model, status="pending"),
        "decision": "audit_error",
    }

    try:
        assert_free_only_model(model)
        if model != DEFAULT_AUDIO_MODEL:
            raise GroqFreeOnlyViolation(f"groq_audio_model_not_approved:{model}")
        expected = narration_from_plan(output_dir / "plan.json")
        with tempfile.TemporaryDirectory(prefix="isco-groq-audio-audit-") as temp_dir:
            audio_path = Path(temp_dir) / "final-16k-mono.flac"
            extractor(output_dir / "final.mp4", audio_path)
            actual, governor = transcriber(audio_path, api_key=api_key or "", model=model)
        comparison = compare_transcripts(expected, actual)
        document.update(
            {
                "groq_governor": governor,
                "transcription": {
                    "model": model,
                    "language": "ar",
                    "text": actual,
                },
                "comparison": comparison,
                "decision": comparison["decision"],
            }
        )
    except GroqRateLimited as exc:
        document.update(
            {
                "groq_governor": _governor_telemetry(model, status="rate_limited", http_status=429),
                "decision": "audit_skipped",
                "audit_error": str(exc),
            }
        )
    except Exception as exc:
        document.update(
            {
                "groq_governor": _governor_telemetry(model, status="error"),
                "decision": "audit_error",
                "audit_error": f"{type(exc).__name__}: {str(exc)[:240]}",
            }
        )

    try:
        _write_audit(audit_path, document)
    except Exception as exc:
        print(f"Groq Audio Audit write skipped ({type(exc).__name__}); production unchanged")
        return document

    print(f"Groq Audio Audit: decision={document['decision']} observe_only; production unchanged")
    return document
