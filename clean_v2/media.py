from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import statistics
from dataclasses import dataclass
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .visual_story import compact_searchable_visual_intent


MAX_MEDIA_BYTES = 160 * 1024 * 1024
MAX_SEARCH_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TTS_AUDIO_BYTES = 64 * 1024 * 1024

CHARON_MAX_ATTEMPTS = 3
CHARON_RETRY_DELAYS_SECONDS = (1.0, 2.0)
MAX_SHORT_TTS_RETRY_AFTER_SECONDS = 10.0

GEMINI38_TTS_MODEL = "gemini-3.8-flash-tts"
GEMINI38_LITE_TTS_MODEL = "gemini-3.8-flash-lite-tts"
_GEMINI38_ALLOWED_TTS_MODELS = frozenset({GEMINI38_TTS_MODEL, GEMINI38_LITE_TTS_MODEL})
GEMINI38_PRIMARY_VOICE = "Charon"
GEMINI38_QUESTIONER_VOICE = "Orus"
GEMINI38_PROVIDER = "gemini-3.8:Charon"
GEMINI38_REFERENCE_PROFILE = "gemini-3.8-flash-tts:Charon:Orus"
# Reviewed by ear against flash-tts:Charon: on gemini-3.8-flash-lite-tts,
# Algenib reads better in Arabic than lite-Charon. Orus stays the fixed
# questioner voice on both models; only the primary/answering voice changes
# when the whole narration falls back to the lite pass.
GEMINI38_LITE_PRIMARY_VOICE = "Algenib"
GEMINI38_LITE_PROVIDER = "gemini-3.8:Algenib"
GEMINI38_LITE_REFERENCE_PROFILE = "gemini-3.8-flash-lite-tts:Algenib:Orus"
GEMINI38_NARRATOR_STYLE = (
    "Natural Modern Standard Arabic adult narrator. Warm, mature, intelligent and conversational; "
    "calm confidence, human pacing, clear articulation, no announcer tone."
)
GEMINI38_QUESTIONER_STYLE = (
    "Natural Modern Standard Arabic questioner. Concise, intelligent and curious; "
    "firm but calm, never theatrical."
)
GEMINI38_INNER_REFLECTIVE_STYLE = (
    "Warm, mature and intimate; subtle hesitation before questions, calm self-correction in answers, "
    "human pacing, no announcer tone, no theatrical acting, no second character."
)
GEMINI38_INNER_RESOLVED_STYLE = (
    "Same exact Charon speaker and identity. Slightly clearer and steadier as the thought resolves, "
    "still private and conversational, never motivational-speaker delivery."
)
GEMINI38_LISTENER_PROXY_STYLE = (
    "Natural Modern Standard Arabic listener voice. Brief, curious and personally invested, as if voicing "
    "the listener's own question or objection; warm and spontaneous, never interviewer-like, theatrical or performative."
)
GEMINI38_PODCAST_ANSWER_STYLE = (
    "Same established Charon channel voice. Calm, close and thoughtful, answering one listener directly; "
    "simple-deep conversational delivery, unhurried but not sleepy, never announcer or lecture tone."
)
GEMINI38_PERFORMANCE_STYLES = {
    "why_reframe": (
        "Same Charon identity. Clear and lightly incisive: open the mistaken frame with controlled tension, "
        "then become warmer and steadier as the useful reframe lands. Never salesy or preachy."
    ),
    "micro_story": (
        "Same Charon identity. Natural story-bearing cadence with concrete forward motion and small pauses at real turns; "
        "intimate and observant, never dramatic acting."
    ),
    "quote_reflection": (
        "Same Charon identity. Measured and spacious with restrained emphasis around the approved quote and its meaning; "
        "calm, clear and reflective without becoming sad or solemn."
    ),
    "direct_cinematic": (
        "Same Charon identity. Grounded forward-moving narration: confident, warm and clear, with gradual lift toward the payoff."
    ),
    "question_answer": (
        "Same Charon identity. Curious self-questioning followed by clear explanatory answers; vary question and answer cadence naturally, "
        "never sound like an FAQ host."
    ),
    "problem_reveal_solution": (
        "Same Charon identity. Focused and concrete: controlled concern on the problem, sharper clarity on the mechanism, "
        "then practical calm on the resolution."
    ),
    "story_analysis": (
        "Same Charon identity. Observational narrative warmth through the scene, then a slightly more analytical but still human tone "
        "when extracting meaning; no documentary announcer delivery."
    ),
    "paradox": (
        "Same Charon identity. Calm intellectual tension when holding two apparently conflicting ideas, then measured confidence as the contradiction resolves."
    ),
    "hypothesis_test": (
        "Same Charon identity. Curious and evidence-minded, lightly provisional during the test and measured at the conclusion; "
        "never overstate certainty."
    ),
    "connected_list": (
        "Same Charon identity. Clear cumulative momentum where each reason or step adds weight; avoid numbered-list cadence or punchy listicle delivery."
    ),
}

_DIALOGUE_LABEL_RE = re.compile(r"(?m)^\s*([AB]):\s*\S")

# Visual-pacing bounds for splitting one section's own narration-weighted
# estimated duration across several distinct same-query clips instead of one
# clip lingering for the whole section. Numeric philosophy borrowed from the
# legacy Engine's M7 adaptive pacing (_MIN_ADAPTIVE_SHOT_SECONDS /
# MAX_SHOTS_PER_SCENE), not its semantic-director machinery - Clean V2 has no
# beat/scene/candidate pipeline to drive that, so this is the plain
# arithmetic equivalent.
PACING_MAX_SHOT_SECONDS = 22.0
PACING_MIN_SHOT_SECONDS = 3.5
PACING_MAX_SHOTS_PER_SECTION = 3

# Short visuals are semantic-story owned: normally 3-5 real scenes total.
# Measured voice owns timing only; duration never fabricates extra shots.
SHORT_CUT_DISSOLVE_SECONDS = 0.12
SHORT_HOOK_MAX_SINGLE_SHOT_SECONDS = 5.0
SHORT_HOOK_SECOND_SHOT_TRIGGER_SECONDS = 4.0
SHORT_MASTER_LOOK_FILTER = (
    "eq=contrast=1.04:saturation=0.90,"
    "colorbalance=rs=0.015:gs=0.003:bs=-0.012"
)
SHORT_LOCAL_AI_STILL_MAX_BYTES = 20 * 1024 * 1024
SHORT_LOCAL_AI_STILL_SECONDS = 8.0
AI_STILL_CLIP_SECONDS = 12.0
SHORT_MIN_COLOR_SATURATION_AVG = 5.0
COVERR_MAX_SEARCHES_PER_RUN = 12
STOCK_PRIMARY_PAGE_SIZE = 24


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_secret(name: str) -> str:
    direct = str(os.environ.get(name) or "").strip()
    if direct:
        return direct
    secret_file = str(os.environ.get(f"{name}_FILE") or "").strip()
    if not secret_file:
        return ""
    path = Path(secret_file)
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tts_http_status(exc: BaseException) -> int | None:
    direct = getattr(exc, "http_status", None)
    response = getattr(exc, "response", None)
    candidates = (
        direct,
        getattr(response, "status_code", None),
        getattr(exc, "code", None),
    )
    for candidate in candidates:
        try:
            status = int(candidate)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599:
            return status
    match = re.search(
        r"(?:status(?:_code)?|http)[^0-9]{0,12}([1-5][0-9]{2})",
        str(exc),
        re.I,
    )
    return int(match.group(1)) if match else None


def _tts_retry_after_seconds(exc: BaseException) -> float | None:
    direct = getattr(exc, "retry_after_seconds", None)
    headers = getattr(exc, "headers", None)
    response = getattr(exc, "response", None)
    if headers is None:
        headers = getattr(response, "headers", None)
    raw_header: object = None
    if headers is not None:
        try:
            raw_header = headers.get("Retry-After") or headers.get("retry-after")
        except (AttributeError, TypeError):
            raw_header = None
    values = [direct, raw_header]
    match = re.search(r"retry-after\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)", str(exc), re.I)
    if match:
        values.append(match.group(1))
    for value in values:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _charon_retry_delay(exc: BaseException, retry_index: int) -> float | None:
    """Return a safe same-provider delay, or None when retrying would violate evidence."""
    status = _tts_http_status(exc)
    if status in {400, 401, 403, 404}:
        return None
    retry_after = _tts_retry_after_seconds(exc)
    if retry_after is not None:
        if retry_after > MAX_SHORT_TTS_RETRY_AFTER_SECONDS:
            return None
        return retry_after
    index = min(max(0, retry_index), len(CHARON_RETRY_DELAYS_SECONDS) - 1)
    return CHARON_RETRY_DELAYS_SECONDS[index]


def _tts_exception_detail(exc: BaseException, *, limit: int = 200) -> str:
    """Bounded, single-line detail for a TTS exception whose type alone
    isn't diagnostic.

    Engine's synthesize_wav wraps every underlying Gemini TTS failure -
    auth, quota, network, model access, content refusal - in one generic
    RuntimeError built from its own safe_error() helper (type name and any
    HTTP status only, no request/secret data), so type(exc).__name__ alone
    is always "RuntimeError" no matter the real cause. That detail exists
    only in str(exc); this just surfaces it instead of silently dropping it.
    """
    text = " ".join(str(exc).split())
    return text[:limit]


def _tts_failure_reason(exc: BaseException | None, *, missing: str) -> str:
    if exc is None:
        return missing
    status = _tts_http_status(exc)
    reason = type(exc).__name__
    detail = _tts_exception_detail(exc)
    if detail and detail != reason:
        reason = f"{reason}({detail})"
    return f"{reason}_http_{status}" if status is not None else reason


def _gemini38_dialogue_turns(transcript: str) -> list[tuple[str, str]]:
    """Parse A:/B: turns without ever exposing speaker labels to the TTS model.

    The runtime identity injector may place the prayer/channel sentence between
    dialogue turns. Unlabelled text is therefore owned by the main B/Charon voice.
    """
    source = str(transcript or "").strip()
    if not source:
        return []
    marker = re.compile(r"(^|\n|\s)([AB]):\s+", re.M)
    matches = list(marker.finditer(source))
    if not matches:
        return []

    turns: list[tuple[str, str]] = []
    prefix = source[: matches[0].start()].strip()
    if prefix:
        turns.append(("B", prefix))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        spoken = source[start:end].strip()
        if spoken:
            turns.append((match.group(2), spoken))
    if not turns:
        raise RuntimeError("Clean V2 dialogue voice contract has no spoken turns")
    return turns


def _spoken_voice_roles(transcript: str) -> dict[str, str]:
    """Bind every supported narration shape to the fixed Gemini voice roster."""
    turns = _gemini38_dialogue_turns(transcript)
    if turns:
        return {
            "mode": "dialogue_qa",
            "questioner": GEMINI38_QUESTIONER_VOICE,
            "responder": GEMINI38_PRIMARY_VOICE,
        }
    return {"mode": "single_narrator", "narrator": GEMINI38_PRIMARY_VOICE}


def _gemini38_synthesize(
    api_key: str,
    transcript: str,
    output_path: Path,
    *,
    model: str,
    primary_voice: str,
    questioner_voice: str,
    performance_mode: str = "",
) -> Path:
    """Call Gemini 3.8 TTS directly over REST; no SDK upgrade is required."""
    if model not in _GEMINI38_ALLOWED_TTS_MODELS:
        raise RuntimeError(
            f"Clean V2 Gemini TTS model drift: allowed={sorted(_GEMINI38_ALLOWED_TTS_MODELS)} actual={model}"
        )
    if not api_key:
        raise RuntimeError("Clean V2 Gemini TTS requires GEMINI_API_KEY")

    turns = _gemini38_dialogue_turns(transcript)
    if turns:
        listener_proxy = str(performance_mode or "") == "podcast_listener_proxy_qa"
        content: list[dict[str, Any]] = []
        for speaker, spoken in turns:
            content.append(
                {
                    "type": "text",
                    "text": spoken,
                    "annotations": [
                        {
                            "type": "speech_metadata",
                            "speaker": speaker,
                            "style": (
                                (GEMINI38_LISTENER_PROXY_STYLE if listener_proxy else GEMINI38_QUESTIONER_STYLE)
                                if speaker == "A"
                                else (GEMINI38_PODCAST_ANSWER_STYLE if listener_proxy else GEMINI38_NARRATOR_STYLE)
                            ),
                        }
                    ],
                }
            )
        speech_config: Any = {
            "mode": "conversational",
            "speakers": [
                {"speaker": "A", "voice": questioner_voice},
                {"speaker": "B", "voice": primary_voice},
            ],
        }
    else:
        source = transcript.strip()
        if str(performance_mode or "") == "inner_dialogue":
            sentences = [
                item.strip()
                for item in re.split(r"(?<=[.!؟!])\s+", source)
                if item.strip()
            ]
            if not sentences:
                sentences = [source]
            content = []
            for index, sentence in enumerate(sentences):
                style = (
                    GEMINI38_INNER_RESOLVED_STYLE
                    if index == len(sentences) - 1
                    else GEMINI38_INNER_REFLECTIVE_STYLE
                )
                content.append(
                    {
                        "type": "text",
                        "text": sentence,
                        "annotations": [
                            {
                                "type": "speech_metadata",
                                "style": style,
                            }
                        ],
                    }
                )
        else:
            resolved_style = GEMINI38_PERFORMANCE_STYLES.get(
                str(performance_mode or ""),
                GEMINI38_NARRATOR_STYLE,
            )
            content = [
                {
                    "type": "text",
                    "text": source,
                    "annotations": [
                        {
                            "type": "speech_metadata",
                            "style": resolved_style,
                        }
                    ],
                }
            ]
        speech_config = [{"voice": primary_voice}]

    payload = {
        "model": model,
        "input": [{"type": "user_input", "content": content}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": speech_config},
    }
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
            "User-Agent": "Isco-Clean-V2/1",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read((MAX_TTS_AUDIO_BYTES * 2) + 1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise TtsProviderError(
            f"gemini_3_8_http_{int(exc.code)}",
            http_status=int(exc.code),
            retry_after_seconds=_tts_retry_after_seconds(exc),
        ) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise TtsProviderError(
            f"gemini_3_8_transport_{type(exc).__name__.lower()}"
        ) from None

    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Gemini 3.8 TTS returned invalid JSON") from exc

    encoded = ""
    steps = decoded.get("steps") if isinstance(decoded, dict) else None
    if isinstance(steps, list):
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            blocks = step.get("content")
            if not isinstance(blocks, list):
                continue
            for block in reversed(blocks):
                if (
                    isinstance(block, dict)
                    and str(block.get("type") or "") == "audio"
                    and isinstance(block.get("data"), str)
                ):
                    encoded = str(block["data"])
                    break
            if encoded:
                break
    if not encoded:
        raise RuntimeError("Gemini 3.8 TTS returned no audio payload")

    try:
        audio = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise RuntimeError("Gemini 3.8 TTS returned invalid base64 audio") from exc

    if len(audio) < 1024 or len(audio) > MAX_TTS_AUDIO_BYTES:
        raise RuntimeError("Gemini 3.8 TTS returned an invalid audio size")
    if not audio.startswith(b"RIFF"):
        raise RuntimeError("Gemini 3.8 TTS unary output is not WAV/RIFF")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.gemini38.tmp.wav")
    temporary.write_bytes(audio)
    try:
        with wave.open(str(temporary), "rb") as wav:
            if (
                wav.getnchannels() != 1
                or wav.getsampwidth() != 2
                or wav.getframerate() != 24000
                or wav.getnframes() <= 0
            ):
                raise RuntimeError("Gemini 3.8 TTS returned an unexpected WAV format")
        os.replace(temporary, output_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return output_path


class TtsProviderError(RuntimeError):
    def __init__(
        self,
        reason: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.reason = str(reason or "tts_provider_error")
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self.reason)


class VoiceInfrastructureError(RuntimeError):
    """Terminal Gemini 3.8 voice failure after bounded same-provider retries."""

    def __init__(
        self,
        *,
        charon_attempts: int,
        charon_reason: str,
        secondary_reason: str = "gemini_3_8_only_fail_closed_no_fallback",
    ) -> None:
        self.charon_attempts = int(charon_attempts)
        self.charon_reason = str(charon_reason or "unknown")
        self.secondary_reason = str(secondary_reason or "gemini_3_8_only_fail_closed_no_fallback")
        self.fallback_used = False
        super().__init__(
            "CLEAN_V2_VOICE_INFRASTRUCTURE "
            f"reason=gemini_3_8_unavailable attempts={self.charon_attempts} "
            f"error={self.charon_reason} fallback=false"
        )


class GeminiOnlyVoiceSynthesizer:
    """Gemini 3.8 Flash TTS family only.

    Tries exactly one model per call: whichever `tts_model` this instance is
    currently configured with. It never substitutes a different model or
    vendor mid-call - any exhausted cloud failure after bounded same-model
    retries raises VoiceInfrastructureError and fails that call closed.

    The questioner voice (Orus) is fixed on both models. The primary/
    answering voice is Charon on gemini-3.8-flash-tts, but Algenib on
    gemini-3.8-flash-lite-tts - reviewed by ear and picked because it reads
    better in Arabic than lite-Charon on that model.

    A narration made of several of these calls (one per section/chunk) must
    never end up mixing gemini-3.8-flash-tts and gemini-3.8-flash-lite-tts
    audio in the same job. That whole-job decision - try the primary model
    throughout, and only on exhaustion discard everything and redo the
    entire narration on gemini-3.8-flash-lite-tts instead - belongs to the
    caller (see _synthesize_sectioned_voice), which reconfigures tts_model
    between full passes rather than asking this class to switch mid-job.
    """

    EXPECTED_PRIMARY_VOICE = GEMINI38_PRIMARY_VOICE
    EXPECTED_QUESTIONER_VOICE = GEMINI38_QUESTIONER_VOICE

    def __init__(
        self,
        api_key: str,
        *,
        tts_model: str = GEMINI38_TTS_MODEL,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.tts_model = str(tts_model or "").strip() or GEMINI38_TTS_MODEL
        self.last_provider: str | None = None
        self.fallback_used: bool | None = None
        self.charon_attempts = 0
        self.voice_roles: dict[str, str] | None = None
        self.voice_approval_status: str | None = None
        self.voice_reference_profile: str | None = None

    def synthesize(
        self,
        transcript: str,
        output_path: Path,
        *,
        primary_only: bool = False,
        performance_mode: str = "",
    ) -> Path:
        del primary_only  # Model selection is owned by the caller's whole-job pass, not per call.
        if not transcript.strip():
            raise RuntimeError("cannot synthesize an empty transcript")
        if self.tts_model not in _GEMINI38_ALLOWED_TTS_MODELS:
            raise RuntimeError(
                "Clean V2 Gemini-only TTS model drift: "
                f"allowed={sorted(_GEMINI38_ALLOWED_TTS_MODELS)} actual={self.tts_model}"
            )

        is_lite = self.tts_model == GEMINI38_LITE_TTS_MODEL
        primary_voice = GEMINI38_LITE_PRIMARY_VOICE if is_lite else self.EXPECTED_PRIMARY_VOICE
        questioner_voice = self.EXPECTED_QUESTIONER_VOICE
        provider = GEMINI38_LITE_PROVIDER if is_lite else GEMINI38_PROVIDER
        reference_profile = GEMINI38_LITE_REFERENCE_PROFILE if is_lite else GEMINI38_REFERENCE_PROFILE

        self.voice_roles = _spoken_voice_roles(transcript)
        self.last_provider = None
        self.fallback_used = self.tts_model != GEMINI38_TTS_MODEL
        self.voice_approval_status = None
        self.voice_reference_profile = None
        self.charon_attempts = 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: BaseException | None = None

        if self.api_key:
            for attempt in range(1, CHARON_MAX_ATTEMPTS + 1):
                self.charon_attempts = attempt
                try:
                    _gemini38_synthesize(
                        self.api_key,
                        transcript,
                        output_path,
                        model=self.tts_model,
                        primary_voice=primary_voice,
                        questioner_voice=questioner_voice,
                        performance_mode=performance_mode,
                    )
                    if not output_path.is_file() or output_path.stat().st_size < 1024:
                        raise RuntimeError("Gemini 3.8 TTS produced an empty narration file")
                    self.last_provider = provider
                    self.voice_approval_status = "user_selected_gemini_3_8"
                    self.voice_reference_profile = reference_profile
                    print(
                        f"Clean V2 voice provider selected: {self.last_provider} "
                        f"model={self.tts_model} fallback={self.fallback_used} "
                        f"attempt={attempt}/{CHARON_MAX_ATTEMPTS}"
                    )
                    return output_path
                except Exception as exc:
                    last_error = exc
                    output_path.unlink(missing_ok=True)
                    print(
                        "Clean V2 Gemini TTS attempt failed: "
                        f"model={self.tts_model} attempt={attempt}/{CHARON_MAX_ATTEMPTS} "
                        f"error_type={type(exc).__name__} "
                        f"detail={_tts_exception_detail(exc)}"
                    )
                    if attempt >= CHARON_MAX_ATTEMPTS:
                        break
                    delay = _charon_retry_delay(exc, attempt - 1)
                    if delay is None:
                        break
                    time.sleep(delay)

        reason = _tts_failure_reason(last_error, missing="missing_api_key")
        raise VoiceInfrastructureError(
            charon_attempts=self.charon_attempts,
            charon_reason=reason,
        )


def _get_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Isco-Clean-V2/1", **dict(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_SEARCH_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"http_{int(exc.code)}") from None
    except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"transport_{type(exc).__name__.lower()}") from None
    if len(raw) > MAX_SEARCH_RESPONSE_BYTES:
        raise RuntimeError("search_response_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("search_response_not_json") from None
    if not isinstance(value, dict):
        raise RuntimeError("search_response_not_object")
    return value


def _safe_media_host(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    host = str(parsed.hostname or "").lower()
    allowed = ("pexels.com", "pixabay.com", "coverr.co")
    return parsed.scheme == "https" and any(
        host == suffix or host.endswith(f".{suffix}") for suffix in allowed
    )


def _download_media(url: str, destination: Path) -> None:
    if not _safe_media_host(url):
        raise RuntimeError("unexpected_media_host")
    request = urllib.request.Request(url, headers={"User-Agent": "Isco-Clean-V2/1"})
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as handle:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared and declared > MAX_MEDIA_BYTES:
                raise RuntimeError("media_too_large")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEDIA_BYTES:
                    raise RuntimeError("media_too_large")
                handle.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if total < 1024:
        destination.unlink(missing_ok=True)
        raise RuntimeError("empty_media")


def _validated_local_short_ai_still() -> tuple[Path | None, str | None]:
    """Resolve an optional local AI still without network/provider coupling."""
    raw = str(os.environ.get("CLEAN_V2_SHORT_AI_STILL") or "").strip()
    if not raw:
        return None, None
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None, "invalid_local_ai_still_path"
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return None, "unsupported_local_ai_still_type"
    try:
        size = path.stat().st_size
    except OSError:
        return None, "local_ai_still_missing"
    if size <= 0 or size > SHORT_LOCAL_AI_STILL_MAX_BYTES:
        return None, "local_ai_still_size_invalid"
    return path, None


def _render_local_short_ai_still(source: Path, destination: Path) -> Path:
    """Turn one local still into a restrained zero-cost portrait motion clip."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,"
        "zoompan=z='min(pzoom+0.0006,1.06)':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        "d=1:s=1080x1920:fps=30,format=yuv420p"
    )
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(source),
            "-vf",
            vf,
            "-t",
            f"{SHORT_LOCAL_AI_STILL_SECONDS:g}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        timeout=180,
    )
    if not destination.is_file() or destination.stat().st_size < 1024:
        destination.unlink(missing_ok=True)
        raise RuntimeError("local_ai_still_render_failed")
    return destination


def _render_ai_still(source: Path, destination: Path, *, fmt: str) -> Path:
    """Turn one generated still into a restrained clip for the shared renderer."""
    if fmt == "short":
        width, height = 1080, 1920
    elif fmt in {"film", "podcast"}:
        width, height = 1920, 1080
    else:
        raise RuntimeError("ai_still_render_format_unsupported")
    frames = max(1, int(round(AI_STILL_CLIP_SECONDS * 30.0)))
    zoom = 0.025
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        f"zoompan=z='min({1.0 + zoom:.6f},1+{zoom:.6f}*on/{frames})':"
        "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d=1:s={width}x{height}:fps=30,format=yuv420p"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(source),
            "-vf",
            vf,
            "-t",
            f"{AI_STILL_CLIP_SECONDS:g}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        timeout=240,
    )
    if not destination.is_file() or destination.stat().st_size < 1024:
        destination.unlink(missing_ok=True)
        raise RuntimeError("ai_still_render_failed")
    return destination


def _prepare_ai_reference(source: Path, destination: Path) -> Path:
    """Create the sub-512px reference required by FLUX.2 Klein."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            (
                "scale=480:480:force_original_aspect_ratio=decrease,"
                "pad=480:480:(ow-iw)/2:(oh-ih)/2:color=black"
            ),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(destination),
        ],
        timeout=90,
    )
    if not destination.is_file() or destination.stat().st_size < 1024:
        destination.unlink(missing_ok=True)
        raise RuntimeError("ai_still_reference_failed")
    return destination


def _ai_still_prompt(
    visual_story: Mapping[str, Any],
    beat: Mapping[str, Any],
    *,
    fmt: str,
    with_reference: bool,
) -> str:
    thread = visual_story.get("retention_thread")
    thread = thread if isinstance(thread, Mapping) else {}
    orientation = "vertical 9:16" if fmt == "short" else "horizontal 16:9"
    visual_world = str(visual_story.get("visual_world") or "").strip()[:320]
    motif = str(thread.get("visual_motif") or "").strip()[:180]
    viewer_intent = str(beat.get("viewer_intent") or "").strip()[:240]
    meaning_target = str(beat.get("meaning_target") or viewer_intent).strip()[:240]
    must_have = ", ".join(str(item) for item in (beat.get("semantic_must_have") or []))[:240]
    should_avoid = ", ".join(str(item) for item in (beat.get("semantic_should_avoid") or []))[:220]
    scene = str(beat.get("shot_intent") or "").strip()[:260]
    role = str(beat.get("role") or "").strip()
    hook_visual_rule = (
        "HOOK FRAME: make the first frame visually arresting but truthful to the exact topic. "
        "Show an immediate observable tension, interrupted action, unusual state, visible consequence, "
        "or decisive moment; favor close/medium framing, asymmetry, depth, and strong local focal contrast. "
        "Do not use a passive calm establishing shot, generic desk, coffee cup, window-gazing, slow walking, "
        "or typing unless that exact action is the semantic tension. "
        if role == "hook"
        else ""
    )
    reference_rule = (
        "Use input image 0 as the exact environment/style anchor; preserve its location, "
        "palette, practical lighting, lens language, textures, and recurring motif. "
        if with_reference
        else "Establish one distinctive coherent environment that can be reused later. "
    )
    core = (
        f"Cinematic photorealistic {orientation} frame for an Arabic self-development video. "
        f"Visual world: {visual_world}. "
        f"Recurring motif: {motif}. "
        f"Beat role: {role}. "
        f"{hook_visual_rule}"
        f"Viewer intent: {viewer_intent}. "
        f"Specific meaning target: {meaning_target}. Must visibly include: {must_have}. "
        f"Avoid generic substitutes: {should_avoid}. Scene: {scene}. "
        f"{reference_rule}"
        "Lived-in foreground, midground and background depth, restrained deep navy/charcoal shadow world, "
        "soft practical light, ivory-neutral highlights and very limited warm-gold accents; never a blue wash. "
        "One clear focal action, clean negative space for Arabic overlay. For hook only, use stronger "
        "local subject contrast and a more immediate decisive composition; body/payoff stay restrained."
    )
    mandatory_tail = (
        "IMAGE ONLY: do not render any caption, title, subtitle, word, letter, Arabic text, or logo. "
        "If a screen or interface is required by the semantic cue, keep its shapes abstract and all copy illegible; "
        "otherwise avoid interface elements. "
        "No identifiable faces; hands, back view, objects, or environment only. "
        "No readable text, letters, logos, watermarks, collage, split screen, fantasy glow, "
        "or exaggerated advertising look."
    )
    core = " ".join(core.split())
    mandatory_tail = " ".join(mandatory_tail.split())
    core_budget = max(0, 2048 - len(mandatory_tail) - 1)
    return (core[:core_budget].rstrip() + " " + mandatory_tail).strip()


def _pexels_file(video: Mapping[str, Any], *, portrait: bool) -> Mapping[str, Any] | None:
    files = [item for item in (video.get("video_files") or []) if isinstance(item, dict) and item.get("link")]
    if not files:
        return None

    def score(item: Mapping[str, Any]) -> tuple[int, int, int]:
        width, height = int(item.get("width") or 0), int(item.get("height") or 0)
        orientation = int((height > width) if portrait else (width >= height))
        pixels = width * height
        usable = int(pixels >= 640 * 360)
        target = (720 * 1280) if portrait else (1280 * 720)
        distance = -abs(pixels - target)
        return orientation, usable, distance

    return max(files, key=score)


def _short_visual_color_compatible(path: Path) -> tuple[bool, str | None]:
    """Reject only near-monochrome Short stock; leave normal footage to the existing grade."""
    try:
        from isco_video_agent.media.color import measure_color_stats
    except Exception:
        return True, None
    try:
        stats = measure_color_stats(Path(path))
    except Exception:
        return True, None
    saturation = float(stats.saturation_avg)
    if saturation < SHORT_MIN_COLOR_SATURATION_AVG:
        return False, f"short_near_monochrome saturation_avg={saturation:.2f}"
    return True, None


_STOCK_INTENT_DROP_TOKENS = frozenset({
    "cinematic", "warm", "neutral", "lighting", "light", "shot", "frame",
    "composition", "depth", "foreground", "background", "soft", "natural",
})


def _specific_beat_stock_query(value: object) -> str:
    """Reuse the Beat's own English visual intent as the stock query when safe.

    This restores the simple #866 behavior: no new model call, no new search pass,
    and no semantic layer. Localized or overly vague/empty intent falls back to the
    dedicated stock_query_en authored in Planning.
    """
    return compact_searchable_visual_intent(
        value,
        drop_tokens=_STOCK_INTENT_DROP_TOKENS,
    )


# Keep retrieval semantic. Visual drama is judged by existing Visual QA/rendering,
# not by stuffing generic cinematic adjectives into the stock search.
HOOK_STOCK_RETRIEVAL_SUFFIX = "close up"


def _hook_stock_retrieval_query(query: str, beat: Mapping[str, Any]) -> str:
    """Strengthen only the hook retrieval without adding another provider request."""
    compact = " ".join(str(query or "").split()).strip()
    if str(beat.get("role") or "").strip() != "hook" or not compact:
        return compact
    lowered = compact.lower()
    missing = [word for word in HOOK_STOCK_RETRIEVAL_SUFFIX.split() if word not in lowered]
    return " ".join([compact, *missing])[:260].strip()


CHANNEL_STOCK_QUERY_SUFFIX = ""


def _channel_stock_query(query: str) -> str:
    """Keep provider search semantic; channel styling is enforced after acquisition.

    Historical searches appended ``warm neutral cinematic``. Provider docs and live
    retrieval showed those generic style words consume scarce query space without
    proving the action/state. The renderer already owns grade/composition, so stock
    search now preserves only the concrete scene semantics.
    """
    return " ".join(str(query or "").split()).strip()


def _stock_query_ladder(primary_query: str, beat: Mapping[str, Any]) -> tuple[str, ...]:
    """Return at most two meaning-preserving searches for one visual beat.

    Planning may author one deliberately different real-world alternate. Old/resumed
    artifacts without it get one local fallback from a concrete semantic cue. This is
    bounded search evolution, not an open retry loop and it adds no model call.
    """
    raw_candidates: list[object] = [primary_query, beat.get("stock_query_alt_en")]
    if not str(beat.get("stock_query_alt_en") or "").strip():
        raw_candidates.extend(list(beat.get("semantic_must_have") or [])[:2])
        raw_candidates.append(beat.get("stock_query_en"))

    queries: list[str] = []
    seen: set[str] = set()
    for raw in raw_candidates:
        compact = compact_searchable_visual_intent(
            raw,
            drop_tokens=_STOCK_INTENT_DROP_TOKENS,
            max_words=10,
        )
        if not compact:
            continue
        key = " ".join(re.findall(r"[a-z0-9]+", compact.casefold()))
        if not key or key in seen:
            continue
        seen.add(key)
        queries.append(compact[:160])
        if len(queries) >= 2:
            break
    if not queries:
        fallback = " ".join(str(primary_query or "").split()).strip()
        return (fallback[:160],) if fallback else ()
    return tuple(queries)


_STOCK_RANK_STOP_TOKENS = frozenset({
    "cinematic", "warm", "neutral", "natural", "practical", "light", "lighting",
    "close", "up", "wide", "shot", "frame", "strong", "focal", "contrast",
    "no", "face", "visible", "only", "soft", "depth", "dark", "bright",
})


def _stock_metadata_semantic_score(query: str, metadata: str) -> float:
    """Cheap semantic tie-breaker using metadata already returned by the same API call."""
    query_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", str(query or "").casefold())
        if len(token) > 2 and token not in _STOCK_RANK_STOP_TOKENS
    }
    if not query_tokens:
        return 0.0
    metadata_tokens = set(
        re.findall(r"[a-z0-9]+", str(metadata or "").replace("-", " ").casefold())
    )
    matched = len(query_tokens & metadata_tokens)
    return min(1.0, matched / float(min(6, max(1, len(query_tokens)))))


def _stock_local_rank_score(
    *,
    index: int,
    count: int,
    width: int,
    height: int,
    duration: float,
    portrait: bool,
    query: str = "",
    metadata: str = "",
) -> float:
    """Rank one existing result page locally; no extra provider/search call."""
    count = max(1, int(count))
    provider_relevance = 1.0 - (max(0, int(index)) / count)
    semantic = _stock_metadata_semantic_score(query, metadata)
    orientation_ok = (height > width) if portrait else (width >= height)
    pixels = min(max(0, width * height), 1920 * 1080) / float(1920 * 1080)
    duration_fit = min(1.0, max(0.0, float(duration)) / 4.0)
    return (
        provider_relevance * 0.42
        + semantic * 0.23
        + (1.0 if orientation_ok else 0.0) * 0.15
        + pixels * 0.15
        + duration_fit * 0.05
    )


def _timeline_hook_end_seconds(output_dir: Path) -> float:
    path = Path(output_dir) / "timeline-first.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0.0
    rows = payload.get("identity_events") if isinstance(payload, Mapping) else None
    for row in rows or []:
        if isinstance(row, Mapping) and str(row.get("kind") or "") == "hook":
            try:
                return max(0.0, float(row.get("end") or 0.0))
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _enforce_short_hook_shot_cap(
    paths: list[Path],
    durations: list[float],
    section_ids: list[str] | None,
    *,
    hook_seconds: float,
) -> tuple[list[Path], list[float]]:
    """Force one visible change inside a long Short hook without new media calls."""
    if (
        hook_seconds <= SHORT_HOOK_SECOND_SHOT_TRIGGER_SECONDS
        or section_ids is None
        or len(paths) != len(durations)
        or len(paths) != len(section_ids)
        or len(paths) < 2
    ):
        return list(paths), list(durations)

    first_section = section_ids[0]
    if section_ids[1] != first_section:
        return list(paths), list(durations)

    result_paths = list(paths)
    result_durations = list(durations)
    first_budget = min(
        SHORT_HOOK_MAX_SINGLE_SHOT_SECONDS,
        max(PACING_MIN_SHOT_SECONDS, hook_seconds * 0.55),
    )
    if result_durations[0] > first_budget:
        moved = result_durations[0] - first_budget
        result_durations[0] = first_budget
        result_durations[1] += moved
    return result_paths, result_durations


class StockVisualSource:
    def __init__(
        self,
        *,
        query_normalizer: Callable[[str], str] | None = None,
        media_preflight: Callable[[Path], dict[str, Any] | None] | None = None,
        media_transform: Callable[[Path], Path] | None = None,
    ) -> None:
        self.events: list[dict[str, Any]] = []
        self._used: set[tuple[str, str]] = set()
        self.query_normalizer = query_normalizer
        self.media_preflight = media_preflight
        self.media_transform = media_transform
        self._coverr_search_calls = 0

    def _event(
        self,
        provider: str,
        query: str,
        result: str,
        *,
        wire_attempted: bool,
        reason: str | None = None,
    ) -> None:
        self.events.append(
            {
                "timestamp": _utc_now(),
                "provider": provider,
                "query": query[:200],
                "result": result,
                "wire_attempted": wire_attempted,
                "reason": reason,
            }
        )

    def _coverr(self, query: str, *, portrait: bool) -> dict[str, Any] | None:
        key = _read_secret("COVERR_API_KEY")
        if not key:
            self._event("coverr", query, "unavailable", wire_attempted=False, reason="missing_api_key")
            return None
        if self._coverr_search_calls >= COVERR_MAX_SEARCHES_PER_RUN:
            self._event(
                "coverr",
                query,
                "skipped",
                wire_attempted=False,
                reason="run_search_budget_exhausted",
            )
            return None
        self._coverr_search_calls += 1
        params = urllib.parse.urlencode(
            {
                "query": query[:160],
                "page_size": STOCK_PRIMARY_PAGE_SIZE,
                "sort": "popular",
                "urls": "true",
            }
        )
        try:
            body = _get_json(
                f"https://api.coverr.co/videos?{params}",
                headers={"Authorization": f"Bearer {key}"},
            )
            hits = [item for item in (body.get("hits") or []) if isinstance(item, dict)]
            ranked: list[tuple[float, dict[str, Any], str, tuple[str, str]]] = []
            count = max(1, len(hits))
            for index, hit in enumerate(hits):
                asset_id = str(hit.get("id") or "").strip()
                identity = ("coverr", asset_id)
                if not asset_id or identity in self._used:
                    continue
                if bool(hit.get("is_ai") or hit.get("ai_generated")):
                    continue
                source_type = str(hit.get("source") or hit.get("type") or "").casefold()
                if source_type in {"ai", "generated", "ai_generated"}:
                    continue
                urls = hit.get("urls") or {}
                download_url = str(
                    (urls.get("mp4_download") if isinstance(urls, Mapping) else "")
                    or (urls.get("mp4") if isinstance(urls, Mapping) else "")
                    or ""
                ).strip()
                if not download_url:
                    continue
                width = int(hit.get("max_width") or 0)
                height = int(hit.get("max_height") or 0)
                if not width or not height:
                    vertical = bool(hit.get("is_vertical"))
                    width, height = ((1080, 1920) if vertical else (1920, 1080))
                metadata = " ".join(
                    [
                        str(hit.get("title") or ""),
                        str(hit.get("description") or ""),
                        " ".join(str(x) for x in (hit.get("tags") or []) if x),
                        " ".join(str(x) for x in (hit.get("search_keywords") or []) if x),
                    ]
                )
                score = _stock_local_rank_score(
                    index=index,
                    count=count,
                    width=width,
                    height=height,
                    duration=float(hit.get("duration") or 0.0),
                    portrait=portrait,
                    query=query,
                    metadata=metadata,
                )
                ranked.append((score, hit, download_url, identity))
            if ranked:
                score, hit, download_url, identity = max(ranked, key=lambda item: item[0])
                self._used.add(identity)
                self._event(
                    "coverr",
                    query,
                    "selected_ranked",
                    wire_attempted=True,
                    reason=f"score={score:.3f}",
                )
                return {
                    "provider": "coverr",
                    "asset_id": identity[1],
                    "download_url": download_url,
                    "source_url": str(hit.get("url") or "https://coverr.co"),
                    "creator": "Coverr",
                    "creator_url": "https://coverr.co",
                    "query": query,
                    "media_kind": "video",
                    "attribution_required": True,
                }
            self._event("coverr", query, "empty", wire_attempted=True)
        except Exception as exc:
            self._event(
                "coverr",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return None

    def _pexels_photo(self, query: str, *, portrait: bool) -> dict[str, Any] | None:
        key = _read_secret("PEXELS_API_KEY")
        if not key:
            self._event("pexels_photo", query, "unavailable", wire_attempted=False, reason="missing_api_key")
            return None
        params = urllib.parse.urlencode(
            {
                "query": query[:200],
                "orientation": "portrait" if portrait else "landscape",
                "size": "large",
                "per_page": STOCK_PRIMARY_PAGE_SIZE,
                "locale": "en-US",
            }
        )
        try:
            body = _get_json(
                f"https://api.pexels.com/v1/search?{params}",
                headers={"Authorization": key},
            )
            photos = [item for item in (body.get("photos") or []) if isinstance(item, dict)]
            ranked: list[tuple[float, dict[str, Any], str, tuple[str, str]]] = []
            count = max(1, len(photos))
            for index, photo in enumerate(photos):
                identity = ("pexels_photo", str(photo.get("id") or ""))
                if not identity[1] or identity in self._used:
                    continue
                src = photo.get("src") or {}
                if not isinstance(src, Mapping):
                    continue
                image_url = str(
                    src.get("large2x")
                    or src.get("large")
                    or src.get("portrait" if portrait else "landscape")
                    or src.get("original")
                    or ""
                ).strip()
                if not image_url:
                    continue
                width = int(photo.get("width") or 0)
                height = int(photo.get("height") or 0)
                score = _stock_local_rank_score(
                    index=index,
                    count=count,
                    width=width,
                    height=height,
                    duration=4.0,
                    portrait=portrait,
                    query=query,
                    metadata=" ".join(
                        [str(photo.get("alt") or ""), str(photo.get("url") or "")]
                    ),
                )
                ranked.append((score, photo, image_url, identity))
            if ranked:
                score, photo, image_url, identity = max(ranked, key=lambda item: item[0])
                self._used.add(identity)
                self._event(
                    "pexels_photo",
                    query,
                    "selected_ranked",
                    wire_attempted=True,
                    reason=f"score={score:.3f}",
                )
                return {
                    "provider": "pexels",
                    "asset_id": identity[1],
                    "download_url": image_url,
                    "source_url": str(photo.get("url") or ""),
                    "creator": str(photo.get("photographer") or ""),
                    "creator_url": str(photo.get("photographer_url") or ""),
                    "query": query,
                    "media_kind": "photo",
                }
            self._event("pexels_photo", query, "empty", wire_attempted=True)
        except Exception as exc:
            self._event(
                "pexels_photo",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return None

    def _pixabay_photo(self, query: str, *, portrait: bool) -> dict[str, Any] | None:
        key = _read_secret("PIXABAY_API_KEY")
        if not key:
            self._event("pixabay_photo", query, "unavailable", wire_attempted=False, reason="missing_api_key")
            return None
        params = urllib.parse.urlencode(
            {
                "key": key,
                "q": query[:100],
                "image_type": "photo",
                "orientation": "vertical" if portrait else "horizontal",
                "safesearch": "true",
                "order": "popular",
                "per_page": STOCK_PRIMARY_PAGE_SIZE,
            }
        )
        try:
            body = _get_json(f"https://pixabay.com/api/?{params}")
            hits = [item for item in (body.get("hits") or []) if isinstance(item, dict)]
            ranked: list[tuple[float, dict[str, Any], str, tuple[str, str]]] = []
            count = max(1, len(hits))
            for index, hit in enumerate(hits):
                identity = ("pixabay_photo", str(hit.get("id") or ""))
                if not identity[1] or identity in self._used:
                    continue
                image_url = str(
                    hit.get("largeImageURL")
                    or hit.get("webformatURL")
                    or hit.get("previewURL")
                    or ""
                ).strip()
                if not image_url:
                    continue
                score = _stock_local_rank_score(
                    index=index,
                    count=count,
                    width=int(hit.get("imageWidth") or hit.get("webformatWidth") or 0),
                    height=int(hit.get("imageHeight") or hit.get("webformatHeight") or 0),
                    duration=4.0,
                    portrait=portrait,
                    query=query,
                    metadata=" ".join(
                        [str(hit.get("tags") or ""), str(hit.get("pageURL") or "")]
                    ),
                )
                ranked.append((score, hit, image_url, identity))
            if ranked:
                score, hit, image_url, identity = max(ranked, key=lambda item: item[0])
                self._used.add(identity)
                self._event(
                    "pixabay_photo",
                    query,
                    "selected_ranked",
                    wire_attempted=True,
                    reason=f"score={score:.3f}",
                )
                return {
                    "provider": "pixabay",
                    "asset_id": identity[1],
                    "download_url": image_url,
                    "source_url": str(hit.get("pageURL") or ""),
                    "creator": str(hit.get("user") or ""),
                    "creator_url": "",
                    "query": query,
                    "media_kind": "photo",
                }
            self._event("pixabay_photo", query, "empty", wire_attempted=True)
        except Exception as exc:
            self._event(
                "pixabay_photo",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return None

    def _pexels(self, query: str, *, portrait: bool) -> dict[str, Any] | None:
        key = _read_secret("PEXELS_API_KEY")
        if not key:
            self._event("pexels", query, "unavailable", wire_attempted=False, reason="missing_api_key")
            return None
        params = urllib.parse.urlencode(
            {
                "query": query[:200],
                "orientation": "portrait" if portrait else "landscape",
                "size": "medium",
                "per_page": 12,
                "locale": "en-US",
            }
        )
        try:
            body = _get_json(
                f"https://api.pexels.com/v1/videos/search?{params}",
                headers={"Authorization": key},
            )
            videos = [item for item in (body.get("videos") or []) if isinstance(item, dict)]
            ranked: list[tuple[float, dict[str, Any], Mapping[str, Any], tuple[str, str]]] = []
            count = max(1, len(videos))
            for index, video in enumerate(videos):
                identity = ("pexels", str(video.get("id") or ""))
                selected = _pexels_file(video, portrait=portrait)
                if identity in self._used or selected is None:
                    continue
                width = int(selected.get("width") or 0)
                height = int(selected.get("height") or 0)
                duration = float(video.get("duration") or 0.0)
                ranked.append((
                    _stock_local_rank_score(
                        index=index,
                        count=count,
                        width=width,
                        height=height,
                        duration=duration,
                        portrait=portrait,
                        query=query,
                        metadata=str(video.get("url") or ""),
                    ),
                    video,
                    selected,
                    identity,
                ))
            if ranked:
                _, video, selected, identity = max(ranked, key=lambda item: item[0])
                self._used.add(identity)
                self._event("pexels", query, "selected_ranked", wire_attempted=True)
                user = video.get("user") or {}
                return {
                    "provider": "pexels",
                    "asset_id": identity[1],
                    "download_url": str(selected.get("link")),
                    "source_url": str(video.get("url") or ""),
                    "creator": str(user.get("name") or ""),
                    "creator_url": str(user.get("url") or ""),
                    "query": query,
                }
            self._event("pexels", query, "empty", wire_attempted=True)
        except Exception as exc:
            self._event(
                "pexels",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return None

    def _pixabay(self, query: str, *, portrait: bool) -> dict[str, Any] | None:
        key = _read_secret("PIXABAY_API_KEY")
        if not key:
            self._event("pixabay", query, "unavailable", wire_attempted=False, reason="missing_api_key")
            return None
        params = urllib.parse.urlencode(
            {
                "key": key,
                "q": query[:100],
                "video_type": "film",
                "safesearch": "true",
                "per_page": 12,
            }
        )
        try:
            body = _get_json(f"https://pixabay.com/api/videos/?{params}")
            hits = [item for item in (body.get("hits") or []) if isinstance(item, dict)]
            ranked: list[tuple[float, dict[str, Any], Mapping[str, Any], tuple[str, str]]] = []
            count = max(1, len(hits))
            for index, hit in enumerate(hits):
                identity = ("pixabay", str(hit.get("id") or ""))
                if identity in self._used:
                    continue
                variants = hit.get("videos") or {}
                candidates = [
                    item
                    for name in ("medium", "small", "large", "tiny")
                    for item in [variants.get(name) if isinstance(variants, dict) else None]
                    if isinstance(item, dict) and item.get("url")
                ]
                if not candidates:
                    continue
                selected = max(
                    candidates,
                    key=lambda item: _stock_local_rank_score(
                        index=index,
                        count=count,
                        width=int(item.get("width") or 0),
                        height=int(item.get("height") or 0),
                        duration=float(hit.get("duration") or 0.0),
                        portrait=portrait,
                    ),
                )
                ranked.append((
                    _stock_local_rank_score(
                        index=index,
                        count=count,
                        width=int(selected.get("width") or 0),
                        height=int(selected.get("height") or 0),
                        duration=float(hit.get("duration") or 0.0),
                        portrait=portrait,
                        query=query,
                        metadata=" ".join(
                            str(item or "")
                            for item in (hit.get("tags"), hit.get("pageURL"))
                        ),
                    ),
                    hit,
                    selected,
                    identity,
                ))
            if ranked:
                _, hit, selected, identity = max(ranked, key=lambda item: item[0])
                self._used.add(identity)
                self._event("pixabay", query, "selected_ranked", wire_attempted=True)
                return {
                    "provider": "pixabay",
                    "asset_id": identity[1],
                    "download_url": str(selected.get("url")),
                    "source_url": str(hit.get("pageURL") or ""),
                    "creator": str(hit.get("user") or ""),
                    "creator_url": "",
                    "query": query,
                }
            self._event("pixabay", query, "empty", wire_attempted=True)
        except Exception as exc:
            self._event(
                "pixabay",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return None

    def acquire(
        self,
        plan: Mapping[str, Any],
        output_dir: Path,
        fmt: str,
        max_visuals: int,
        section_estimated_seconds: Mapping[str, float] | None = None,
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        portrait = fmt in {"moment", "story", "short"}
        clips: list[Path] = []
        rights: list[dict[str, Any]] = []
        sections = list(plan.get("sections") or [])[: max(1, int(max_visuals))]

        # One provider-backed visual per semantic beat. The beat list is shared by
        # long and short formats and is produced during Planning. The current
        # section_estimated_seconds input is intentionally ignored for scene count;
        # it is retained in the method signature only for Phase-A compatibility and
        # later render-time allocation.
        del section_estimated_seconds
        section_by_id = {
            str(section.get("id") or ""): section
            for section in sections
            if isinstance(section, Mapping)
        }
        raw_story = plan.get("visual_story")
        raw_beats = (
            list(raw_story.get("beats") or [])
            if isinstance(raw_story, Mapping)
            else []
        )
        beats: list[dict[str, Any]] = []
        section_beat_counts: dict[str, int] = {}
        for index, raw_beat in enumerate(raw_beats, start=1):
            if not isinstance(raw_beat, Mapping):
                continue
            section_id = str(raw_beat.get("section_id") or "").strip()
            if section_id not in section_by_id:
                continue
            section = section_by_id[section_id]
            shot_intent = str(raw_beat.get("shot_intent") or "").strip()
            if not shot_intent:
                shot_intent = str(section.get("visual_query_en") or "").strip()

            beat_ordinal = section_beat_counts.get(section_id, 0)
            primary_query = str(section.get("visual_query_en") or "").strip()
            alternate_query = str(section.get("visual_query_alt_en") or "").strip()
            fallback_query = (
                alternate_query
                if beat_ordinal % 2 == 1 and alternate_query
                else primary_query
            )
            section_beat_counts[section_id] = beat_ordinal + 1

            # Fresh Phase-B stories own one retrieval query per semantic beat.
            # Old resumable artifacts without that field retain the latest
            # English-intent/section-query fallback instead of regressing to
            # localized text at the stock-provider boundary.
            stock_query_en = str(raw_beat.get("stock_query_en") or "").strip()
            if not stock_query_en:
                normalized_intent = " ".join(shot_intent.split()).strip()
                stock_query_en = (
                    normalized_intent
                    if normalized_intent
                    and normalized_intent.isascii()
                    and any(char.isalpha() for char in normalized_intent)
                    else fallback_query
                )
            if not stock_query_en:
                continue

            beats.append(
                {
                    "id": str(raw_beat.get("id") or f"b{index}").strip(),
                    "section_id": section_id,
                    "viewer_intent": str(raw_beat.get("viewer_intent") or "").strip(),
                    "meaning_target": str(raw_beat.get("meaning_target") or raw_beat.get("viewer_intent") or "").strip(),
                    "semantic_must_have": list(raw_beat.get("semantic_must_have") or []),
                    "semantic_should_avoid": list(raw_beat.get("semantic_should_avoid") or []),
                    "shot_intent": shot_intent,
                    "stock_query_en": stock_query_en,
                    "writer_anchor_ar": str(raw_beat.get("writer_anchor_ar") or "").strip(),
                    "role": str(raw_beat.get("role") or "").strip(),
                    "source_preference": str(
                        raw_beat.get("source_preference") or "stock_motion"
                    ).strip(),
                }
            )

        # Compatibility path for old plans/tests that predate visual-story.json:
        # exactly one semantic beat per section, never duration-derived splitting.
        if not beats:
            for index, section in enumerate(sections, start=1):
                beats.append(
                    {
                        "id": f"b{index}",
                        "section_id": str(section.get("id") or ""),
                        "viewer_intent": str(section.get("purpose") or "").strip(),
                        "shot_intent": str(section.get("visual_query_en") or "").strip(),
                        "stock_query_en": str(section.get("visual_query_en") or "").strip(),
                        "source_preference": "stock_motion",
                    }
                )

        ai_hook_reference: Path | None = None
        ai_route_available = True

        def _acquire_one(
            query: str,
            section_id: str,
            beat: Mapping[str, Any],
            *,
            auxiliary: bool,
        ) -> bool:
            nonlocal ai_hook_reference, ai_route_available
            wants_ai = str(beat.get("source_preference") or "") == "ai_still"
            if wants_ai and ai_route_available:
                # AI is an optional visual anchor, never a required dependency.
                # The provider module proves zero-cost eligibility before inference.
                from clean_v2.ai_still import (
                    CloudflareAIStillUnavailable,
                    generate_cloudflare_ai_still,
                )

                beat_id = str(beat.get("id") or f"b{len(clips) + 1}")
                still_dir = output_dir / ".ai-stills"
                still = still_dir / f"{beat_id}.image"
                destination = output_dir / f"visual-{len(clips) + 1:02d}.mp4"
                prompt = _ai_still_prompt(
                    raw_story if isinstance(raw_story, Mapping) else {},
                    beat,
                    fmt=fmt,
                    with_reference=(
                        str(beat.get("role") or "") == "payoff"
                        and ai_hook_reference is not None
                    ),
                )
                try:
                    provenance = generate_cloudflare_ai_still(
                        prompt=prompt,
                        destination=still,
                        fmt=fmt,
                        reference=(
                            ai_hook_reference
                            if str(beat.get("role") or "") == "payoff"
                            else None
                        ),
                    )
                    _render_ai_still(still, destination, fmt=fmt)
                    if self.media_preflight is not None:
                        blocked = self.media_preflight(destination)
                        if blocked is not None:
                            raise CloudflareAIStillUnavailable(
                                "security_v1_block:"
                                + str(
                                    blocked.get("local_media_rejection")
                                    or "security_v1_block"
                                )[:80]
                            )
                    if fmt == "short":
                        color_ok, color_reason = _short_visual_color_compatible(
                            destination
                        )
                        if not color_ok:
                            raise CloudflareAIStillUnavailable(
                                f"short_color_rejected:{color_reason}"
                            )
                    if self.media_transform is not None:
                        destination = Path(self.media_transform(destination))
                    next_hook_reference = ai_hook_reference
                    if (
                        str(beat.get("role") or "") == "hook"
                        and next_hook_reference is None
                    ):
                        next_hook_reference = _prepare_ai_reference(
                            still, still_dir / "hook-continuity-reference.jpg"
                        )
                except Exception as exc:
                    # One unavailable free AI route must never block production.
                    # Disable later AI attempts for this run and fall back to stock.
                    ai_route_available = False
                    destination.unlink(missing_ok=True)
                    still.unlink(missing_ok=True)
                    reason = str(exc)
                    local_failure = any(
                        marker in reason
                        for marker in (
                            "feature_flag_disabled",
                            "credentials_unavailable",
                            "account_id_malformed",
                            "prompt_invalid",
                            "format_unsupported",
                        )
                    )
                    self._event(
                        "cloudflare_workers_ai",
                        query,
                        "fallback_to_stock",
                        wire_attempted=not local_failure,
                        reason=reason[:120],
                    )
                else:
                    ai_hook_reference = next_hook_reference
                    candidate = {
                        "provider": "cloudflare_workers_ai",
                        "asset_id": (
                            f"ai-{beat_id}-"
                            f"{str(provenance.get('prompt_sha256') or '')[:16]}"
                        ),
                        "source_url": provenance.get("source_url"),
                        "creator": "Isco Clean V2 / FLUX.2 Klein 4B",
                        "creator_url": provenance.get("source_url"),
                        "query": query,
                        "local_file": destination.name,
                        "section_id": section_id,
                        "beat_id": beat_id,
                        "viewer_intent": str(beat.get("viewer_intent") or ""),
                        "shot_intent": str(beat.get("shot_intent") or query),
                        "writer_anchor_ar": str(beat.get("writer_anchor_ar") or ""),
                        "display_text_ar": str(beat.get("display_text_ar") or ""),
                        "role": str(beat.get("role") or ""),
                        "source_preference": "ai_still",
                        "source_actual": "ai_still",
                        "ai_generated": True,
                        "ai_provenance": dict(provenance),
                    }
                    if auxiliary:
                        candidate["pacing_auxiliary"] = True
                        candidate["story_beat_auxiliary"] = True
                    clips.append(destination)
                    rights.append(candidate)
                    self._event(
                        "cloudflare_workers_ai",
                        query,
                        "selected",
                        wire_attempted=True,
                    )
                    return True
            elif wants_ai:
                self._event(
                    "cloudflare_workers_ai",
                    query,
                    "fallback_to_stock",
                    wire_attempted=False,
                    reason="ai_route_disabled_after_prior_failure",
                )

            for finder in (self._pexels, self._pixabay):
                candidate = finder(query, portrait=portrait)
                if candidate is None:
                    continue
                destination = output_dir / f"visual-{len(clips) + 1:02d}.mp4"
                try:
                    _download_media(str(candidate["download_url"]), destination)
                except Exception as exc:
                    self._event(
                        str(candidate["provider"]),
                        query,
                        "download_failed",
                        wire_attempted=True,
                        reason=str(exc)[:80],
                    )
                    continue
                if self.media_preflight is not None:
                    blocked = self.media_preflight(destination)
                    if blocked is not None:
                        self._event(
                            str(candidate["provider"]),
                            query,
                            "security_blocked",
                            wire_attempted=False,
                            reason=str(blocked.get("local_media_rejection") or "security_v1_block")[:80],
                        )
                        destination.unlink(missing_ok=True)
                        continue
                if fmt == "short":
                    color_ok, color_reason = _short_visual_color_compatible(destination)
                    if not color_ok:
                        self._event(
                            str(candidate["provider"]),
                            query,
                            "color_rejected",
                            wire_attempted=False,
                            reason=color_reason,
                        )
                        destination.unlink(missing_ok=True)
                        continue
                if self.media_transform is not None:
                    destination = Path(self.media_transform(destination))
                candidate = {
                    key: value
                    for key, value in candidate.items()
                    if key != "download_url"
                }
                candidate["local_file"] = destination.name
                candidate["section_id"] = section_id
                candidate["beat_id"] = str(beat.get("id") or "")
                candidate["viewer_intent"] = str(beat.get("viewer_intent") or "")
                candidate["meaning_target"] = str(beat.get("meaning_target") or beat.get("viewer_intent") or "")
                candidate["semantic_must_have"] = list(beat.get("semantic_must_have") or [])
                candidate["semantic_should_avoid"] = list(beat.get("semantic_should_avoid") or [])
                candidate["shot_intent"] = str(beat.get("shot_intent") or query)
                candidate["writer_anchor_ar"] = str(beat.get("writer_anchor_ar") or "")
                candidate["display_text_ar"] = str(beat.get("display_text_ar") or "")
                candidate["role"] = str(beat.get("role") or "")
                candidate["source_preference"] = str(
                    beat.get("source_preference") or "stock_motion"
                )
                candidate["source_actual"] = "stock_motion"
                if auxiliary:
                    # Keep this compatibility flag because existing render/opening
                    # code uses it to distinguish the first section visual. Its cause
                    # is now a real story beat, not timing-based pacing.
                    candidate["pacing_auxiliary"] = True
                    candidate["story_beat_auxiliary"] = True
                clips.append(destination)
                rights.append(candidate)
                return True
            return False

        seen_sections: set[str] = set()
        for beat in beats:
            section_id = str(beat.get("section_id") or "")
            specific = _specific_beat_stock_query(beat.get("shot_intent"))
            query = specific or str(beat.get("stock_query_en") or "").strip()
            if not query:
                continue
            query = _hook_stock_retrieval_query(query, beat)
            if self.query_normalizer is not None:
                query = self.query_normalizer(query)
            query = _channel_stock_query(query)
            auxiliary = section_id in seen_sections
            if _acquire_one(query, section_id, beat, auxiliary=auxiliary):
                seen_sections.add(section_id)

        if not clips:
            fallback = output_dir / "visual-fallback.mp4"
            create_fallback_visual(fallback, portrait=portrait)
            if self.media_transform is not None:
                fallback = Path(self.media_transform(fallback))
            clips.append(fallback)
            fallback_section = str(sections[0].get("id") or "") if sections else ""
            rights.append(
                {
                    "provider": "generated_local",
                    "asset_id": "clean-v2-fallback",
                    "source_url": None,
                    "creator": "Isco Clean V2",
                    "creator_url": None,
                    "query": None,
                    "local_file": fallback.name,
                    "section_id": fallback_section,
                }
            )
            self._event(
                "generated_local",
                "",
                "selected",
                wire_attempted=False,
                reason="stock_unavailable",
            )
        return clips, rights

    def _pexels_recovery_pool(
        self,
        query: str,
        *,
        portrait: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        key = _read_secret("PEXELS_API_KEY")
        if not key:
            self._event(
                "pexels",
                query,
                "unavailable",
                wire_attempted=False,
                reason="missing_api_key",
            )
            return []
        params = urllib.parse.urlencode(
            {
                "query": query[:200],
                "orientation": "portrait" if portrait else "landscape",
                "size": "medium",
                "per_page": max(12, int(limit)),
                "locale": "en-US",
            }
        )
        candidates: list[dict[str, Any]] = []
        try:
            body = _get_json(
                f"https://api.pexels.com/v1/videos/search?{params}",
                headers={"Authorization": key},
            )
            for video in body.get("videos") or []:
                if not isinstance(video, dict):
                    continue
                identity = ("pexels", str(video.get("id") or ""))
                selected = _pexels_file(video, portrait=portrait)
                if identity in self._used or selected is None:
                    continue
                user = video.get("user") or {}
                candidates.append(
                    {
                        "provider": "pexels",
                        "asset_id": identity[1],
                        "download_url": str(selected.get("link")),
                        "source_url": str(video.get("url") or ""),
                        "creator": str(user.get("name") or ""),
                        "creator_url": str(user.get("url") or ""),
                        "query": query,
                    }
                )
                if len(candidates) >= max(1, int(limit)):
                    break
            self._event(
                "pexels",
                query,
                "recovery_pool_ready" if candidates else "empty",
                wire_attempted=True,
                reason=f"candidates={len(candidates)}",
            )
        except Exception as exc:
            self._event(
                "pexels",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return candidates

    def _pixabay_recovery_pool(
        self,
        query: str,
        *,
        portrait: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        key = _read_secret("PIXABAY_API_KEY")
        if not key:
            self._event(
                "pixabay",
                query,
                "unavailable",
                wire_attempted=False,
                reason="missing_api_key",
            )
            return []
        params = urllib.parse.urlencode(
            {
                "key": key,
                "q": query[:100],
                "video_type": "film",
                "safesearch": "true",
                "per_page": max(12, int(limit)),
            }
        )
        candidates: list[dict[str, Any]] = []
        try:
            body = _get_json(f"https://pixabay.com/api/videos/?{params}")
            for hit in body.get("hits") or []:
                if not isinstance(hit, dict):
                    continue
                identity = ("pixabay", str(hit.get("id") or ""))
                if identity in self._used:
                    continue
                variants = hit.get("videos") or {}
                variants_list = [
                    item
                    for name in ("medium", "small", "large", "tiny")
                    for item in [
                        variants.get(name) if isinstance(variants, dict) else None
                    ]
                    if isinstance(item, dict) and item.get("url")
                ]
                if not variants_list:
                    continue
                oriented = [
                    item
                    for item in variants_list
                    if (
                        (
                            int(item.get("height") or 0)
                            > int(item.get("width") or 0)
                        )
                        if portrait
                        else (
                            int(item.get("width") or 0)
                            >= int(item.get("height") or 0)
                        )
                    )
                ]
                selected = (oriented or variants_list)[0]
                candidates.append(
                    {
                        "provider": "pixabay",
                        "asset_id": identity[1],
                        "download_url": str(selected.get("url")),
                        "source_url": str(hit.get("pageURL") or ""),
                        "creator": str(hit.get("user") or ""),
                        "creator_url": "",
                        "query": query,
                    }
                )
                if len(candidates) >= max(1, int(limit)):
                    break
            self._event(
                "pixabay",
                query,
                "recovery_pool_ready" if candidates else "empty",
                wire_attempted=True,
                reason=f"candidates={len(candidates)}",
            )
        except Exception as exc:
            self._event(
                "pixabay",
                query,
                "failed",
                wire_attempted=True,
                reason=str(exc)[:80],
            )
        return candidates

    def acquire_replacement_candidates(
        self,
        query: str,
        output_dir: Path,
        fmt: str,
        *,
        destination_name: str,
        section_id: str,
        max_candidates: int = 3,
        exclude_provider: str | None = None,
        exclude_asset_id: object | None = None,
        exclude_assets: list[tuple[str, object]] | None = None,
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Return up to three safe alternate-query candidates with a fixed bound.

        Recovery performs exactly one Pexels search and one Pixabay search, preserves
        each provider's own relevance ordering, interleaves the two pools, and admits
        at most max_candidates downloaded candidates. Security V1 and the existing
        media transform run before a candidate can reach cloud Visual QA.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        portrait = fmt in {"moment", "story", "short"}
        bounded_limit = max(1, min(3, int(max_candidates)))
        normalized_query = str(query or "").strip()
        if self.query_normalizer is not None and normalized_query:
            normalized_query = self.query_normalizer(normalized_query)
        normalized_query = _channel_stock_query(normalized_query)
        if not normalized_query:
            return []

        if exclude_provider and exclude_asset_id is not None:
            self._used.add((str(exclude_provider), str(exclude_asset_id)))
        for provider, asset_id in exclude_assets or []:
            if provider and asset_id is not None:
                self._used.add((str(provider), str(asset_id)))

        destination = output_dir / str(destination_name)
        if not destination.name or destination.parent != output_dir:
            raise RuntimeError("replacement_destination_invalid")

        # Pull a slightly wider local pool so download/security rejections can still
        # leave up to three candidates for Visual QA without another provider search.
        per_provider_limit = bounded_limit * 2
        pexels = self._pexels_recovery_pool(
            normalized_query,
            portrait=portrait,
            limit=per_provider_limit,
        )
        pixabay = self._pixabay_recovery_pool(
            normalized_query,
            portrait=portrait,
            limit=per_provider_limit,
        )
        provider_pools = (pexels, pixabay)
        interleaved: list[dict[str, Any]] = []
        position = 0
        while len(interleaved) < per_provider_limit * 2:
            added = False
            for pool in provider_pools:
                if position < len(pool):
                    interleaved.append(pool[position])
                    added = True
            if not added:
                break
            position += 1

        admitted: list[tuple[Path, dict[str, Any]]] = []
        for ordinal, candidate in enumerate(interleaved, start=1):
            if len(admitted) >= bounded_limit:
                break
            provider = str(candidate.get("provider") or "unknown")
            asset_id = str(candidate.get("asset_id") or "")
            identity = (provider, asset_id)
            if not asset_id or identity in self._used:
                continue
            self._used.add(identity)

            safe_asset = re.sub(r"[^A-Za-z0-9_-]+", "-", asset_id)[:48] or "asset"
            temporary = output_dir / (
                f".{destination.stem}.semantic-recovery-{ordinal:02d}-"
                f"{provider}-{safe_asset}{destination.suffix}"
            )
            temporary.unlink(missing_ok=True)
            temporary.with_suffix(".m8.json").unlink(missing_ok=True)
            try:
                _download_media(str(candidate["download_url"]), temporary)
                if self.media_preflight is not None:
                    blocked = self.media_preflight(temporary)
                    if blocked is not None:
                        self._event(
                            provider,
                            normalized_query,
                            "recovery_security_blocked",
                            wire_attempted=False,
                            reason=str(
                                blocked.get("local_media_rejection")
                                or "security_v1_block"
                            )[:80],
                        )
                        temporary.unlink(missing_ok=True)
                        continue
                replacement = temporary
                if self.media_transform is not None:
                    replacement = Path(self.media_transform(temporary))
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                temporary.with_suffix(".m8.json").unlink(missing_ok=True)
                self._event(
                    provider,
                    normalized_query,
                    "recovery_failed",
                    wire_attempted=True,
                    reason=str(exc)[:80],
                )
                continue

            row = {
                key: value for key, value in candidate.items() if key != "download_url"
            }
            row["local_file"] = destination.name
            row["section_id"] = str(section_id or "")
            row["source_actual"] = "stock_motion"
            row["semantic_recovery"] = True
            row["semantic_recovery_candidate_index"] = len(admitted) + 1
            admitted.append((replacement, row))
            self._event(
                provider,
                normalized_query,
                "recovery_candidate_ready",
                wire_attempted=True,
                reason=f"candidate={len(admitted)}/{bounded_limit}",
            )

        return admitted

    def acquire_replacement(
        self,
        query: str,
        output_dir: Path,
        fmt: str,
        *,
        destination_name: str,
        section_id: str,
        exclude_provider: str | None = None,
        exclude_asset_id: object | None = None,
        exclude_assets: list[tuple[str, object]] | None = None,
    ) -> tuple[Path, dict[str, Any]] | None:
        """Acquire exactly one alternate-query replacement for an existing visual slot.

        The original file remains untouched until a newly searched candidate has been
        downloaded, passed the existing Security V1 preflight and completed the existing
        media transform. The already-selected asset is explicitly excluded even when
        this source instance was reconstructed from a resume checkpoint.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        portrait = fmt in {"moment", "story", "short"}
        normalized_query = str(query or "").strip()
        if self.query_normalizer is not None and normalized_query:
            normalized_query = self.query_normalizer(normalized_query)
        normalized_query = _channel_stock_query(normalized_query)
        if not normalized_query:
            return None

        if exclude_provider and exclude_asset_id is not None:
            self._used.add((str(exclude_provider), str(exclude_asset_id)))
        for provider, asset_id in exclude_assets or []:
            if provider and asset_id is not None:
                self._used.add((str(provider), str(asset_id)))

        destination = output_dir / str(destination_name)
        if not destination.name or destination.parent != output_dir:
            raise RuntimeError("replacement_destination_invalid")

        for finder in (self._pexels, self._pixabay):
            candidate = finder(normalized_query, portrait=portrait)
            if candidate is None:
                continue
            provider = str(candidate.get("provider") or "unknown")
            temporary = output_dir / (
                f".{destination.stem}.semantic-recovery-{provider}{destination.suffix}"
            )
            temporary.unlink(missing_ok=True)
            temporary.with_suffix(".m8.json").unlink(missing_ok=True)
            try:
                _download_media(str(candidate["download_url"]), temporary)
                if self.media_preflight is not None:
                    blocked = self.media_preflight(temporary)
                    if blocked is not None:
                        self._event(
                            provider,
                            normalized_query,
                            "recovery_security_blocked",
                            wire_attempted=False,
                            reason=str(
                                blocked.get("local_media_rejection")
                                or "security_v1_block"
                            )[:80],
                        )
                        temporary.unlink(missing_ok=True)
                        continue
                replacement = temporary
                if self.media_transform is not None:
                    replacement = Path(self.media_transform(temporary))

            except Exception as exc:
                temporary.unlink(missing_ok=True)
                temporary.with_suffix(".m8.json").unlink(missing_ok=True)
                self._event(
                    provider,
                    normalized_query,
                    "recovery_failed",
                    wire_attempted=True,
                    reason=str(exc)[:80],
                )
                continue

            admitted = {
                key: value for key, value in candidate.items() if key != "download_url"
            }
            admitted["local_file"] = destination.name
            admitted["section_id"] = str(section_id or "")
            admitted["source_actual"] = "stock_motion"
            admitted["semantic_recovery"] = True
            self._event(
                provider,
                normalized_query,
                "recovery_candidate_ready",
                wire_attempted=True,
            )
            return replacement, admitted
        return None

    def commit_replacement(self, replacement: Path, destination: Path) -> Path:
        """Atomically promote an already-admitted recovery candidate into the slot."""
        replacement = Path(replacement)
        destination = Path(destination)
        if not replacement.is_file() or replacement.stat().st_size < 1024:
            raise RuntimeError("replacement_candidate_missing")
        replacement_sidecar = replacement.with_suffix(".m8.json")
        destination_sidecar = destination.with_suffix(".m8.json")
        os.replace(replacement, destination)
        if replacement_sidecar.is_file():
            os.replace(replacement_sidecar, destination_sidecar)
        else:
            destination_sidecar.unlink(missing_ok=True)
        return destination


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"required executable is missing: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "")[-2000:].replace("\n", " ")
        raise RuntimeError(f"{command[0]} failed: {detail}") from None


def concat_wav_parts(inputs: list[Path], output: Path) -> Path:
    """Concatenate PCM WAV parts using the legacy Engine's ffmpeg concat shape."""
    if not inputs:
        raise ValueError("concat_wav_parts requires at least one input")
    output.parent.mkdir(parents=True, exist_ok=True)
    listfile = output.with_name(f".{output.stem}.concat.txt")
    try:
        listfile.write_text(
            "\n".join(f"file '{path.resolve()}'" for path in inputs),
            encoding="utf-8",
        )
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listfile),
                "-c:a",
                "pcm_s16le",
                str(output),
            ],
            timeout=300,
        )
    finally:
        listfile.unlink(missing_ok=True)
    return output


def create_fallback_visual(path: Path, *, portrait: bool) -> Path:
    width, height = ((720, 1280) if portrait else (1280, 720))
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=#172033:s={width}x{height}:r=30:d=12",
            "-vf",
            "fade=t=in:st=0:d=1,fade=t=out:st=11:d=1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-y",
            str(path),
        ],
        timeout=90,
    )
    return path


def probe_duration(path: Path) -> float:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        timeout=60,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        raise RuntimeError("ffprobe returned an invalid duration") from None
    if duration <= 0:
        raise RuntimeError("media duration must be positive")
    return duration


def render_derived_short(
    source_path: Path,
    output_path: Path,
    *,
    start_seconds: float,
    end_seconds: float,
) -> Path:
    """Reframe one already-approved long-form segment to 9:16 with no new media/provider call."""
    start = float(start_seconds)
    end = float(end_seconds)
    duration = end - start
    if start < 0 or duration < 7.0 or duration > 30.0:
        raise RuntimeError(
            f"derived short duration must be 7-30s: start={start:.3f} end={end:.3f}"
        )
    source = Path(source_path)
    if not source.is_file() or source.stat().st_size <= 0:
        raise RuntimeError("derived short source is missing")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = (
        "[0:v]split=2[bg0][fg0];"
        "[bg0]scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,boxblur=20:2[bg];"
        "[fg0]scale=1040:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[v]"
    )
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(source),
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-r",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ],
        timeout=600,
    )
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("derived short render is missing or empty")
    return output


def _rights_manifest_payload(output_dir: Path) -> dict[str, Any] | None:
    manifest_path = output_dir / "rights-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _pacing_section_ids(output_dir: Path, paths: list[Path]) -> list[str] | None:
    """Map each body clip to its section_id via rights-manifest.json.

    Returns None (never partially) when the manifest is missing or does not
    cover every path, so callers fall back to the plain uniform-slot split
    exactly as before - this is read-only evidence lookup, never a hard
    requirement.
    """
    payload = _rights_manifest_payload(output_dir)
    assets = payload.get("assets") if payload is not None else None
    if not isinstance(assets, list):
        return None
    by_local_file = {
        str(item.get("local_file") or ""): str(item.get("section_id") or "")
        for item in assets
        if isinstance(item, dict)
    }
    section_ids = [by_local_file.get(path.name, "") for path in paths]
    if any(not section_id for section_id in section_ids):
        return None
    return section_ids


def _exact_section_seconds_from_timeline(
    output_dir: Path,
) -> dict[str, float] | None:
    """Read measured section durations so visual beats follow the voice timeline."""
    path = Path(output_dir) / "timeline-first.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rows = payload.get("section_events") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list) or not rows:
        return None
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return None
        section_id = str(row.get("section_id") or "").strip()
        try:
            start = float(row.get("start"))
            end = float(row.get("end"))
        except (TypeError, ValueError):
            return None
        if not section_id or end <= start:
            return None
        result[section_id] = end - start
    return result or None


def _section_estimated_seconds_from_manifest(
    output_dir: Path,
) -> dict[str, float] | None:
    """Read pipeline.py's narration-weighted per-section duration estimate.

    Returns None when the manifest predates this field (an older resumed
    checkpoint) or holds anything malformed, so callers fall back to the
    plain equal-share split exactly as before.
    """
    payload = _rights_manifest_payload(output_dir)
    raw = payload.get("estimated_section_seconds") if payload is not None else None
    if not isinstance(raw, dict) or not raw:
        return None
    estimated: dict[str, float] = {}
    for key, value in raw.items():
        try:
            estimated[str(key)] = float(value)
        except (TypeError, ValueError):
            return None
    return estimated


def _section_slot_durations(
    output_dir: Path, paths: list[Path], total_seconds: float, *, pad: float = 0.12
) -> list[float]:
    """Split total_seconds across paths, keeping each section's own
    narration-weighted share intact and only subdividing it among that
    section's own clips.

    A section that acquired extra same-query clips (visual pacing for a long
    section) shares its own section duration across those clips instead of
    shrinking every other section's share just because its clip count grew.
    The section sharing total_seconds' relative proportions comes from
    pipeline.py's narration-character-count estimate when available; a
    manifest without it (or a fully-flat call site with no section evidence
    at all) falls back to the previous flat equal-share split unchanged.
    """
    section_ids = _pacing_section_ids(output_dir, paths)
    if section_ids is None:
        slot = (total_seconds / len(paths)) + pad
        return [slot] * len(paths)

    order: list[str] = []
    counts: dict[str, int] = {}
    for section_id in section_ids:
        if section_id not in counts:
            order.append(section_id)
        counts[section_id] = counts.get(section_id, 0) + 1

    exact = _exact_section_seconds_from_timeline(output_dir)
    estimated = (
        exact
        if exact is not None and all(section_id in exact for section_id in order)
        else _section_estimated_seconds_from_manifest(output_dir)
    )
    if estimated is not None and all(section_id in estimated for section_id in order):
        raw_shares = {section_id: max(0.0, estimated[section_id]) for section_id in order}
        raw_total = sum(raw_shares.values())
        if raw_total > 0:
            # Renormalize the real per-section weights onto whatever time
            # budget this call site is actually distributing (the whole
            # narration, or the remaining seconds after the opening's fixed
            # 7/11/12s slots) so the final total still matches exactly.
            scale = total_seconds / raw_total
            section_share = {
                section_id: raw_shares[section_id] * scale for section_id in order
            }
        else:
            flat_slot = total_seconds / max(1, len(order))
            section_share = {section_id: flat_slot for section_id in order}
    else:
        flat_slot = total_seconds / max(1, len(order))
        section_share = {section_id: flat_slot for section_id in order}

    last_index_for_section = {
        section_id: index for index, section_id in enumerate(section_ids)
    }
    allocated: dict[str, float] = {section_id: 0.0 for section_id in order}
    durations: list[float] = []
    for index, section_id in enumerate(section_ids):
        if index == last_index_for_section[section_id]:
            clip_seconds = section_share[section_id] - allocated[section_id]
        else:
            clip_seconds = section_share[section_id] / counts[section_id]
            allocated[section_id] += clip_seconds
        durations.append(clip_seconds + pad)
    return durations


# Same crossfade duration as the Engine's own M9 live-binding dissolve
# (scripts/m9_live_binding.py::_DISSOLVE_SECONDS).
COHESION_DISSOLVE_SECONDS = 0.36

# Reference Color Match Lite: deterministic, zero-AI, zero-network color cohesion.
# M8 has already normalized admitted media to BT.709/SDR. This layer only aligns
# the creative appearance of stock clips to one representative clip per video.
COLOR_SAMPLE_FPS = "1/4"
COLOR_SAMPLE_WIDTH = 96
COLOR_SAMPLE_MAX_FRAMES = 24
COLOR_MATCH_STRENGTH = 0.62
COLOR_MATCH_SCALE_MIN = 0.88
COLOR_MATCH_SCALE_MAX = 1.12
COLOR_MATCH_OFFSET_MAX = 18.0
MASTER_LOOK_LUT_SIZE = 17
MASTER_LOOK_CONTRAST = 1.075
MASTER_LOOK_SATURATION = 0.84
MASTER_LOOK_WARM_R = -0.006
MASTER_LOOK_WARM_G = -0.003
MASTER_LOOK_WARM_B = 0.008

# One restrained local finishing pass after the shared deep navy/charcoal LUT.
# It uses only FFmpeg on the already-selected pixels: no provider/model/network
# call, no timing change, and no second visual authority.
CINEMATIC_FINISH_VERSION = "clean-v2-navy-depth-finish-v4"
CINEMATIC_FINISH_FILTER = (
    "eq=contrast=1.065:brightness=-0.032:saturation=0.94:gamma=0.97,"
    "unsharp=5:5:0.30:5:5:0.0,"
    "vignette=PI/14"
)


@dataclass(frozen=True)
class _RgbStats:
    mean_r: float
    mean_g: float
    mean_b: float
    std_r: float
    std_g: float
    std_b: float

    def as_dict(self) -> dict[str, float]:
        return {
            "mean_r": round(self.mean_r, 4),
            "mean_g": round(self.mean_g, 4),
            "mean_b": round(self.mean_b, 4),
            "std_r": round(self.std_r, 4),
            "std_g": round(self.std_g, 4),
            "std_b": round(self.std_b, 4),
        }


def _clamp_color(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sample_rgb_stats(path: Path) -> _RgbStats:
    """Measure bounded representative RGB mean/std from sparse downscaled frames.

    This is intentionally clip-level, never per-frame auto grading. The resulting
    transform is constant for the entire clip, which avoids flicker/pumping.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-vf",
                (
                    f"fps={COLOR_SAMPLE_FPS},"
                    f"scale={COLOR_SAMPLE_WIDTH}:-2:flags=area,format=rgb24"
                ),
                "-frames:v",
                str(COLOR_SAMPLE_MAX_FRAMES),
                "-an",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=90,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("required executable is missing: ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("color sampling timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"")[-1000:].decode("utf-8", errors="replace").replace("\n", " ")
        raise RuntimeError(f"ffmpeg color sampling failed: {detail}") from None

    raw = proc.stdout or b""
    if len(raw) < 300 or len(raw) % 3:
        raise RuntimeError("color sampling returned insufficient rgb data")

    channels = (raw[0::3], raw[1::3], raw[2::3])
    values: list[tuple[float, float]] = []
    for channel in channels:
        count = len(channel)
        mean = sum(channel) / count
        variance = max(
            0.0,
            (sum(value * value for value in channel) / count) - (mean * mean),
        )
        values.append((mean, math.sqrt(variance)))
    return _RgbStats(
        mean_r=values[0][0],
        mean_g=values[1][0],
        mean_b=values[2][0],
        std_r=values[0][1],
        std_g=values[1][1],
        std_b=values[2][1],
    )


def _representative_reference(
    measured: Mapping[str, _RgbStats],
) -> str:
    """Choose the real clip closest to the channel's restrained neutral/deep world.

    The previous median-medoid rule could make one warm/beige stock clip the visual
    authority for the whole episode. Keep one real reference, but prefer moderate
    exposure, restrained channel imbalance and useful tonal spread so source stock
    cannot redefine the channel palette.
    """
    if not measured:
        raise ValueError("reference selection requires measured clips")
    rows = list(measured.items())

    def channel_distance(stats: _RgbStats) -> float:
        luma = (
            (0.2126 * stats.mean_r)
            + (0.7152 * stats.mean_g)
            + (0.0722 * stats.mean_b)
        )
        # Target a moderate/deep base rather than bright lifestyle stock.
        exposure_penalty = abs(luma - 128.0) * 1.20
        # Penalize strong warm/cool casts aggressively; stock must not redefine
        # the channel palette just because it is closer to the episode median.
        cast_penalty = (
            abs(stats.mean_r - stats.mean_g) * 1.00
            + abs(stats.mean_g - stats.mean_b) * 0.80
        )
        # Prefer enough local contrast/depth to avoid flat washed-out references.
        spread = (stats.std_r + stats.std_g + stats.std_b) / 3.0
        flat_penalty = max(0.0, 48.0 - spread) * 0.85
        bright_penalty = max(0.0, luma - 150.0) * 1.60
        return exposure_penalty + cast_penalty + flat_penalty + bright_penalty

    return min(rows, key=lambda row: channel_distance(row[1]))[0]


def _reference_match_filter(source: _RgbStats, reference: _RgbStats) -> str:
    """Build one bounded RGB mean/std transfer for the entire source clip."""
    expressions: list[str] = []
    for channel, source_mean, source_std, ref_mean, ref_std in (
        ("r", source.mean_r, source.std_r, reference.mean_r, reference.std_r),
        ("g", source.mean_g, source.std_g, reference.mean_g, reference.std_g),
        ("b", source.mean_b, source.std_b, reference.mean_b, reference.std_b),
    ):
        raw_ratio = (ref_std / source_std) if source_std >= 2.0 else 1.0
        scale = _clamp_color(
            1.0 + ((raw_ratio - 1.0) * COLOR_MATCH_STRENGTH),
            COLOR_MATCH_SCALE_MIN,
            COLOR_MATCH_SCALE_MAX,
        )
        target_mean = source_mean + ((ref_mean - source_mean) * COLOR_MATCH_STRENGTH)
        offset = _clamp_color(
            target_mean - (source_mean * scale),
            -COLOR_MATCH_OFFSET_MAX,
            COLOR_MATCH_OFFSET_MAX,
        )
        expressions.append(
            f"{channel}='clip(val*{scale:.6f}{offset:+.6f},0,255)'"
        )
    return "lutrgb=" + ":".join(expressions)


def _grade_clip_filter(path: Path) -> str:
    """Legacy fallback grade used only when reference matching cannot be measured."""
    try:
        from isco_video_agent.media.color import build_color_filter
    except Exception:
        return ""
    try:
        return build_color_filter(path)
    except Exception:
        return ""


def _build_reference_color_plan(
    paths: list[Path],
    output_dir: Path,
) -> dict[str, str]:
    """Measure once, choose one real reference, and return a constant filter per clip."""
    unique: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        path = Path(raw)
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)

    measured: dict[str, _RgbStats] = {}
    failures: dict[str, str] = {}
    for path in unique:
        try:
            measured[str(path)] = _sample_rgb_stats(path)
        except Exception as exc:
            failures[path.name] = f"{type(exc).__name__}:{str(exc)[:120]}"

    filters: dict[str, str] = {}
    report: dict[str, Any] = {
        "schema_version": 1,
        "source": "clean-v2-reference-color-match-lite",
        "provider_calls_added": 0,
        "ai_calls_added": 0,
        "technical_color_normalization_owner": "M8_BT709_SDR_before_render",
        "method": "channel_anchored_rgb_mean_std_reference_match_v2",
        "match_strength": COLOR_MATCH_STRENGTH,
        "master_look": "channel_deep_neutral_v3",
        "measured_clip_count": len(measured),
        "failures": failures,
    }

    if len(measured) >= 2:
        reference_key = _representative_reference(measured)
        reference = measured[reference_key]
        report["status"] = "applied"
        report["reference_file"] = Path(reference_key).name
        report["reference_stats"] = reference.as_dict()
        rows: list[dict[str, Any]] = []
        for path in unique:
            key = str(path)
            stats = measured.get(key)
            if stats is None:
                fragment = _grade_clip_filter(path)
                filters[key] = fragment
                rows.append(
                    {
                        "file": path.name,
                        "mode": "legacy_fallback",
                        "filter_applied": bool(fragment),
                    }
                )
                continue
            fragment = "" if key == reference_key else _reference_match_filter(stats, reference)
            filters[key] = fragment
            rows.append(
                {
                    "file": path.name,
                    "mode": "reference" if key == reference_key else "reference_match",
                    "stats": stats.as_dict(),
                    "filter_applied": bool(fragment),
                }
            )
        report["clips"] = rows
    else:
        report["status"] = "legacy_fallback"
        report["reference_file"] = None
        report["clips"] = []
        for path in unique:
            fragment = _grade_clip_filter(path)
            filters[str(path)] = fragment
            report["clips"].append(
                {
                    "file": path.name,
                    "mode": "legacy_fallback",
                    "filter_applied": bool(fragment),
                }
            )

    try:
        (Path(output_dir) / "color-match.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return filters


def _master_look_value(r: float, g: float, b: float) -> tuple[float, float, float]:
    """One restrained dark navy/charcoal look shared by every final frame."""
    luma = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
    r = luma + ((r - luma) * MASTER_LOOK_SATURATION)
    g = luma + ((g - luma) * MASTER_LOOK_SATURATION)
    b = luma + ((b - luma) * MASTER_LOOK_SATURATION)

    def contrast(value: float) -> float:
        return 0.5 + ((value - 0.5) * MASTER_LOOK_CONTRAST)

    return (
        _clamp_color(contrast(r) + MASTER_LOOK_WARM_R, 0.0, 1.0),
        _clamp_color(contrast(g) + MASTER_LOOK_WARM_G, 0.0, 1.0),
        _clamp_color(contrast(b) + MASTER_LOOK_WARM_B, 0.0, 1.0),
    )


def _write_master_look_lut(path: Path) -> Path:
    """Write a tiny deterministic Iridas .cube LUT; blue outer, red inner for FFmpeg."""
    size = MASTER_LOOK_LUT_SIZE
    if size < 2:
        raise ValueError("master look LUT size must be at least 2")
    lines = [
        'TITLE "Isco Navy Depth v4"',
        f"LUT_3D_SIZE {size}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    denominator = float(size - 1)
    for b_index in range(size):
        b = b_index / denominator
        for g_index in range(size):
            g = g_index / denominator
            for r_index in range(size):
                r = r_index / denominator
                out_r, out_g, out_b = _master_look_value(r, g, b)
                lines.append(f"{out_r:.7f} {out_g:.7f} {out_b:.7f}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def _ffmpeg_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _trim_and_grade_clip(
    source: Path,
    destination: Path,
    *,
    width: int,
    height: int,
    seconds: float,
    grade_filter: str | None = None,
) -> Path:
    """Use real stock motion once; never restart/boomerang a clip to fill a slot."""
    grade = _grade_clip_filter(source) if grade_filter is None else grade_filter
    source_seconds = max(0.01, probe_duration(source))
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30"
    if grade:
        vf = f"{vf},{grade}"
    hold_seconds = max(0.0, float(seconds) - source_seconds)
    if hold_seconds > 0.01:
        vf = f"{vf},tpad=stop_mode=clone:stop_duration={hold_seconds:.3f}"
    vf = f"{vf},trim=duration={seconds:.3f},setpts=PTS-STARTPTS"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        timeout=300,
    )
    return destination


def _dissolve_pair(
    left: Path, right: Path, destination: Path, *, dissolve_seconds: float = COHESION_DISSOLVE_SECONDS
) -> Path:
    """Crossfade two already-trimmed clips into one continuous segment."""
    left_seconds = probe_duration(left)
    right_seconds = probe_duration(right)
    if left_seconds <= dissolve_seconds or right_seconds <= dissolve_seconds:
        raise RuntimeError("pacing_dissolve_pair_too_short_for_timing_preserving_crossfade")
    half = dissolve_seconds / 2.0
    offset = left_seconds - half
    vf = (
        f"[0:v]tpad=stop_mode=clone:stop_duration={half:.6f},settb=AVTB,setpts=PTS-STARTPTS[v0];"
        f"[1:v]tpad=start_mode=clone:start_duration={half:.6f},settb=AVTB,setpts=PTS-STARTPTS[v1];"
        f"[v0][v1]xfade=transition=fade:duration={dissolve_seconds:.6f}:offset={offset:.6f},"
        "fps=30,setsar=1,format=yuv420p[v]"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(left),
            "-i",
            str(right),
            "-filter_complex",
            vf,
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            str(destination),
        ],
        timeout=300,
    )
    expected = left_seconds + right_seconds
    actual = probe_duration(destination)
    if abs(actual - expected) > 0.18:
        destination.unlink(missing_ok=True)
        raise RuntimeError(
            f"pacing_dissolve_timing_invariant_failed expected={expected:.3f} actual={actual:.3f}"
        )
    return destination


def _build_section_body_segments(
    work_dir: Path,
    paths: list[Path],
    durations: list[float],
    section_ids: list[str] | None,
    *,
    width: int,
    height: int,
    dissolve_seconds: float = COHESION_DISSOLVE_SECONDS,
    grade_filters: Mapping[str, str] | None = None,
) -> list[Path]:
    """Match/trim every body clip, then dissolve adjacent same-section clips."""
    work_dir.mkdir(parents=True, exist_ok=True)
    groups: list[list[int]] = []
    if section_ids is None:
        groups = [[index] for index in range(len(paths))]
    else:
        current: list[int] = []
        current_id: str | None = None
        for index, section_id in enumerate(section_ids):
            if current and section_id != current_id:
                groups.append(current)
                current = []
            current.append(index)
            current_id = section_id
        if current:
            groups.append(current)

    segments: list[Path] = []
    for group_index, group in enumerate(groups):
        trimmed: list[Path] = []
        for member_index, clip_index in enumerate(group):
            source = paths[clip_index]
            grade_filter = (
                grade_filters.get(str(source), "")
                if grade_filters is not None
                else None
            )
            trimmed.append(
                _trim_and_grade_clip(
                    source,
                    work_dir / f"trim-{group_index:02d}-{member_index:02d}.mp4",
                    width=width,
                    height=height,
                    seconds=durations[clip_index],
                    grade_filter=grade_filter,
                )
            )
        merged = trimmed[0]
        for member_index in range(1, len(trimmed)):
            try:
                merged = _dissolve_pair(
                    merged,
                    trimmed[member_index],
                    work_dir / f"dissolve-{group_index:02d}-{member_index:02d}.mp4",
                    dissolve_seconds=dissolve_seconds,
                )
            except RuntimeError:
                segments.append(merged)
                merged = trimmed[member_index]
        segments.append(merged)
    return segments


def render_video(
    narration_path: Path,
    visual_paths: list[Path],
    output_path: Path,
    fmt: str,
) -> Path:
    if not visual_paths:
        raise RuntimeError("render requires at least one visual")
    duration = probe_duration(narration_path)
    portrait = fmt in {"moment", "story", "short"}
    width, height = ((1080, 1920) if portrait else (1920, 1080))
    output_dir = Path(output_path).parent

    timeline: dict[str, Any] = {}
    timeline_path = output_dir / "timeline-first.json"
    if fmt in {"short", "film", "podcast"} and timeline_path.is_file():
        try:
            parsed_timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
            timeline = parsed_timeline if isinstance(parsed_timeline, dict) else {}
        except (OSError, json.JSONDecodeError):
            timeline = {}
        if timeline.get("status") != "pass":
            raise RuntimeError("timeline-first manifest missing or invalid before render")

    opening_report: dict[str, Any] = {}
    opening_path = Path(output_path).parent / "opening-director.json"
    if opening_path.is_file():
        try:
            parsed = json.loads(opening_path.read_text(encoding="utf-8"))
            opening_report = parsed if isinstance(parsed, dict) else {}
        except (OSError, json.JSONDecodeError):
            opening_report = {}

    opening_enabled = (
        opening_report.get("status") == "pass"
        and opening_report.get("mode") == "legacy_first_30_three_audited_shots"
        and len(visual_paths) >= 3
    )
    if opening_enabled:
        slots = opening_report.get("slots") or []
        if not isinstance(slots, list) or len(slots) != 3:
            raise RuntimeError("opening director render requires exactly three audited slots")
        expected_seconds = [7.0, 11.0, 12.0]
        actual_seconds = [
            float(item.get("seconds") or 0.0) if isinstance(item, dict) else 0.0
            for item in slots
        ]
        if actual_seconds != expected_seconds:
            raise RuntimeError("opening director render timing contract drift")
        slot_names = [
            str(item.get("local_file") or "")
            for item in slots
            if isinstance(item, dict)
        ]
        input_names = [Path(item).name for item in visual_paths[:3]]
        if len(slot_names) != 3 or input_names != slot_names:
            raise RuntimeError("opening director render inputs do not match audited shots")

        opening_paths = [Path(item) for item in visual_paths[:3]]
        remaining = max(0.0, duration - 30.0)
        # No fixed upper bound here: a section that needed extra same-query
        # coverage for pacing can legitimately push the body clip count past
        # the old flat "1 clip per section" assumption.
        body_paths = [Path(item) for item in visual_paths[3:]]
        if remaining > 0.25 and not body_paths:
            raise RuntimeError("opening director render requires a body visual after 30 seconds")
        if remaining > 0.25:
            body_durations = _section_slot_durations(
                Path(output_path).parent, body_paths, remaining
            )
            paths = [*opening_paths, *body_paths]
            durations = [7.0, 11.0, 12.0, *body_durations]
        else:
            paths = opening_paths
            durations = [7.0, 11.0, 12.0]
    else:
        # Phase B: Planning beats own scene changes for both Film and Short.
        # Timeline First owns only timing, so duration never fabricates/repeats shots.
        paths = [Path(item) for item in visual_paths]
        durations = _section_slot_durations(Path(output_path).parent, paths, duration)

    if fmt == "short":
        short_section_ids = _pacing_section_ids(Path(output_path).parent, paths)
        paths, durations = _enforce_short_hook_shot_cap(
            paths,
            durations,
            short_section_ids,
            hook_seconds=_timeline_hook_end_seconds(Path(output_path).parent),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # All stock footage in this render shares one measured reference. M8 has
    # already normalized technical color space; this step aligns appearance only.
    grade_filters = _build_reference_color_plan(paths, output_dir)

    opening_count = 3 if opening_enabled else 0
    opening_paths_for_render = paths[:opening_count]
    opening_durations = durations[:opening_count]
    body_paths_for_render = paths[opening_count:]
    body_durations_for_render = durations[opening_count:]

    work_dir = output_dir / ".cinematic-cohesion"
    if work_dir.is_dir():
        shutil.rmtree(work_dir, ignore_errors=True)
    try:
        if body_paths_for_render:
            body_section_ids = _pacing_section_ids(output_dir, body_paths_for_render)
            body_segments = _build_section_body_segments(
                work_dir,
                body_paths_for_render,
                body_durations_for_render,
                body_section_ids,
                width=width,
                height=height,
                dissolve_seconds=(
                    SHORT_CUT_DISSOLVE_SECONDS
                    if fmt == "short"
                    else COHESION_DISSOLVE_SECONDS
                ),
                grade_filters=grade_filters,
            )
        else:
            body_segments = []

        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        for path in opening_paths_for_render:
            # Opening Director must obey the same one-pass stock rule as the body.
            # Never restart a short source from frame zero to fill an opening slot.
            command.extend(["-i", str(path)])
        for segment in body_segments:
            command.extend(["-i", str(segment)])
        command.extend(["-i", str(narration_path)])

        filters: list[str] = []
        labels: list[str] = []
        input_index = 0
        for opening_path, clip_seconds in zip(opening_paths_for_render, opening_durations):
            label = f"v{input_index}"
            labels.append(f"[{label}]")
            # Opening footage uses the exact same reference-match plan as body
            # footage; timing/asset selection remain untouched.
            grade = grade_filters.get(str(opening_path), "")
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30"
            )
            if grade:
                vf = f"{vf},{grade}"
            source_seconds = max(0.01, probe_duration(opening_path))
            hold_seconds = max(0.0, float(clip_seconds) - source_seconds)
            if hold_seconds > 0.01:
                vf = f"{vf},tpad=stop_mode=clone:stop_duration={hold_seconds:.3f}"
            vf = f"{vf},trim=duration={clip_seconds:.3f},setpts=PTS-STARTPTS"
            filters.append(f"[{input_index}:v]{vf}[{label}]")
            input_index += 1
        for _segment in body_segments:
            label = f"v{input_index}"
            labels.append(f"[{label}]")
            # Already scaled, cropped, graded, trimmed (and dissolved where
            # applicable) by _build_section_body_segments - just reset PTS.
            filters.append(f"[{input_index}:v]setpts=PTS-STARTPTS[{label}]")
            input_index += 1
        master_lut = _write_master_look_lut(work_dir / "navy-charcoal-master-v4.cube")
        filters.append(f"{''.join(labels)}concat=n={input_index}:v=1:a=0[vcat]")
        master_look = (
            f"lut3d=file='{_ffmpeg_filter_path(master_lut)}':interp=tetrahedral"
        )
        if any(str(value or "").strip() for value in grade_filters.values()):
            master_look = f"{master_look},{CINEMATIC_FINISH_FILTER}"
        if timeline:
            filters.append(
                f"[vcat]{master_look},"
                f"tpad=stop_mode=clone:stop_duration={duration:.3f},"
                f"trim=duration={duration:.3f},setpts=PTS-STARTPTS[vout]"
            )
        else:
            filters.append(f"[vcat]{master_look}[vout]")

        duration_limit = (
            ["-t", f"{duration:.3f}"]
            if timeline
            else ["-shortest"]
        )

        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[vout]",
                "-map",
                f"{input_index}:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-movflags",
                "+faststart",
                *duration_limit,
                "-y",
                str(output_dir / ".timeline-body.mp4" if timeline else output_path),
            ]
        )
        _run(command, timeout=1800)
        if timeline:
            from clean_v2.timeline_render import render_identity_composition

            body_path = output_dir / ".timeline-body.mp4"
            try:
                render_identity_composition(
                    body_path,
                    output_path,
                    fmt=fmt,
                    timeline=timeline,
                )
            finally:
                body_path.unlink(missing_ok=True)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return output_path


def inspect_final(path: Path) -> dict[str, Any]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=90,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError("ffprobe final inspection was not JSON") from None
    streams = payload.get("streams") or []
    video_rows = [item for item in streams if item.get("codec_type") == "video"]
    video_streams = len(video_rows)
    audio_streams = sum(1 for item in streams if item.get("codec_type") == "audio")
    width = int((video_rows[0] if video_rows else {}).get("width") or 0)
    height = int((video_rows[0] if video_rows else {}).get("height") or 0)
    duration = float((payload.get("format") or {}).get("duration") or 0)
    size = path.stat().st_size if path.is_file() else 0
    if video_streams < 1 or audio_streams < 1 or duration <= 1 or size < 10_000:
        raise RuntimeError(
            "final file failed structural inspection "
            f"video={video_streams} audio={audio_streams} duration={duration:.3f} size={size}"
        )
    return {
        "schema_version": 1,
        "status": "pass",
        "path": path.name,
        "size_bytes": size,
        "sha256": _sha256(path),
        "duration_seconds": round(duration, 3),
        "width": width,
        "height": height,
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "quality_layers_executed": [],
    }
