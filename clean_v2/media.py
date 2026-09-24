from __future__ import annotations

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
from xml.sax.saxutils import escape as xml_escape


MAX_MEDIA_BYTES = 160 * 1024 * 1024
MAX_SEARCH_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TTS_AUDIO_BYTES = 64 * 1024 * 1024

CHARON_MAX_ATTEMPTS = 3
CHARON_RETRY_DELAYS_SECONDS = (1.0, 2.0)
MAX_SHORT_TTS_RETRY_AFTER_SECONDS = 10.0

# Performance-only direction appended to the already-proven Engine Gemini TTS
# preamble when Clean V2 is using the Short Charon-only contract. It changes no
# words and adds no provider call; it only asks for a cleaner, more immediate
# conversational section onset instead of an announcer-like pickup.
SHORT_CHARON_STYLE = (
    " For short-form narration, begin each section immediately and conversationally "
    "with a clean first-word attack. Keep the opening thought slightly firmer in intent, "
    "but never louder, theatrical, breathless, or announcer-like. Preserve natural clear "
    "Modern Standard Arabic and let punctuation control the pauses."
)

AZURE_F0_VOICE = "ar-OM-AbdullahNeural"
AZURE_F0_LOCALE = "ar-OM"
AZURE_F0_OUTPUT_FORMAT = "riff-24khz-16bit-mono-pcm"
_AZURE_REGION_RE = re.compile(r"^[a-z0-9]+$")
_DIALOGUE_LABEL_RE = re.compile(r"(?m)^\s*([AB]):\s*\S")
VOICE_REFERENCE_PROFILE_PATH = (
    Path(__file__).resolve().parents[1]
    / "voice-profiles"
    / "voice-reference-profile-v1.json"
)
VOICE_REFERENCE_PROFILE_VERSION = "channel-voice-roster-v1"

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

# Rich Short Visual Lite: still exactly three semantic sections, but 6-9
# final shots depending only on measured voice duration. No new AI stage.
SHORT_STOCK_ASSET_MAX = 6
SHORT_VISUAL_MIN = 6
SHORT_VISUAL_TARGET = 7
SHORT_VISUAL_MAX = 9
SHORT_VISUAL_SEVEN_SHOT_THRESHOLD_SECONDS = 30.0
SHORT_VISUAL_EIGHT_SHOT_THRESHOLD_SECONDS = 36.0
SHORT_VISUAL_NINE_SHOT_THRESHOLD_SECONDS = 41.0
SHORT_CUT_DISSOLVE_SECONDS = 0.12
SHORT_MOTION_ZOOM = 0.045
SHORT_MASTER_LOOK_FILTER = (
    "eq=contrast=1.04:saturation=0.90,"
    "colorbalance=rs=0.015:gs=0.003:bs=-0.012"
)
SHORT_LOCAL_AI_STILL_MAX_BYTES = 20 * 1024 * 1024
SHORT_LOCAL_AI_STILL_SECONDS = 8.0
SHORT_MIN_COLOR_SATURATION_AVG = 5.0


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


def verify_voice_assets(model_path: Path, manifest_path: Path | None) -> None:
    config_path = Path(str(model_path) + ".json")
    for required in (model_path, config_path):
        if not required.is_file() or required.stat().st_size < 1024:
            raise RuntimeError(f"missing Piper voice asset: {required.name}")
    if manifest_path is None:
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot read Piper voice manifest") from exc
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        raise RuntimeError("invalid Piper voice manifest")
    for path in (model_path, config_path):
        expected = files.get(path.name)
        if not isinstance(expected, dict):
            raise RuntimeError(f"Piper manifest is missing {path.name}")
        expected_size = int(expected.get("size_bytes") or 0)
        expected_hash = str(expected.get("sha256") or "")
        if path.stat().st_size != expected_size or _sha256(path) != expected_hash:
            raise RuntimeError(f"Piper asset identity mismatch: {path.name}")


def _text_chunks(text: str, maximum: int = 520) -> list[str]:
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[.!؟?؛])\s+|\n+", text.strip())
        if item.strip()
    ]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        pieces = [sentence[index : index + maximum] for index in range(0, len(sentence), maximum)]
        for piece in pieces:
            proposed = f"{current} {piece}".strip()
            if current and len(proposed) > maximum:
                chunks.append(current)
                current = piece
            else:
                current = proposed
    if current:
        chunks.append(current)
    return chunks


class PiperVoiceSynthesizer:
    def __init__(self, model_path: Path, manifest_path: Path | None = None) -> None:
        self.model_path = model_path
        self.manifest_path = manifest_path

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        verify_voice_assets(self.model_path, self.manifest_path)
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise RuntimeError("piper-tts is not installed") from exc
        chunks = _text_chunks(transcript)
        if not chunks:
            raise RuntimeError("cannot synthesize an empty transcript")
        voice = PiperVoice.load(
            str(self.model_path), config_path=str(self.model_path) + ".json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="clean-v2-piper-") as temporary:
            chunk_paths: list[Path] = []
            for index, chunk in enumerate(chunks, start=1):
                chunk_path = Path(temporary) / f"{index:04d}.wav"
                with wave.open(str(chunk_path), "wb") as handle:
                    voice.synthesize_wav(chunk, handle)
                chunk_paths.append(chunk_path)
            params = None
            with wave.open(str(output_path), "wb") as destination:
                for chunk_path in chunk_paths:
                    with wave.open(str(chunk_path), "rb") as source:
                        if params is None:
                            params = source.getparams()
                            destination.setparams(params)
                        elif (
                            source.getnchannels(),
                            source.getsampwidth(),
                            source.getframerate(),
                            source.getcomptype(),
                        ) != (
                            params.nchannels,
                            params.sampwidth,
                            params.framerate,
                            params.comptype,
                        ):
                            raise RuntimeError("Piper chunks have incompatible WAV parameters")
                        destination.writeframes(source.readframes(source.getnframes()))
        if output_path.stat().st_size < 1024:
            raise RuntimeError("Piper produced an empty narration file")
        return output_path


def _legacy_voice_identity() -> tuple[str, str]:
    """Reuse the pinned Engine voice identity owner; do not duplicate its policy here."""
    from isco_video_agent.providers.gemini import _voice_identity

    return _voice_identity()


def _legacy_gemini_synthesize(
    api_key: str,
    transcript: str,
    output_path: Path,
    *,
    model: str,
    voice: str,
    style: str = "",
) -> Path:
    """Reuse the legacy Gemini TTS implementation with exactly one provider attempt."""
    from isco_video_agent.providers.gemini import synthesize_wav

    return synthesize_wav(
        api_key,
        transcript,
        output_path,
        model=model,
        voice=voice,
        style=style,
        attempts=1,
    )


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


def _spoken_voice_roles(transcript: str) -> dict[str, str]:
    """Bind every supported narration shape to the fixed channel voice roster."""
    speakers = set(_DIALOGUE_LABEL_RE.findall(transcript))
    if speakers and speakers != {"A", "B"}:
        raise RuntimeError(
            "Clean V2 dialogue voice contract requires both A: and B: turns"
        )
    if speakers == {"A", "B"}:
        return {
            "mode": "dialogue_qa",
            "questioner": "Orus",
            "responder": "Charon",
        }
    return {"mode": "single_narrator", "narrator": "Charon"}


def _assert_human_approved_voice_reference(
    *,
    tts_model: str,
    primary_voice: str,
    questioner_voice: str,
) -> str:
    """Fail closed if runtime voice identity drifts from the human-approved sample."""
    try:
        profile = json.loads(VOICE_REFERENCE_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Clean V2 human-approved voice reference is unavailable") from exc
    source = profile.get("source") if isinstance(profile, dict) else None
    voices = profile.get("profiles") if isinstance(profile, dict) else None
    primary = voices.get("primary") if isinstance(voices, dict) else None
    questioner = voices.get("questioner") if isinstance(voices, dict) else None
    expected = {
        "profile_version": VOICE_REFERENCE_PROFILE_VERSION,
        "mode": "human_approved_reference",
        "tts_model": tts_model,
        "primary_voice": primary_voice,
        "questioner_voice": questioner_voice,
        "human_approval": f"{primary_voice}=primary; {questioner_voice}=questioner",
    }
    actual = {
        "profile_version": profile.get("profile_version") if isinstance(profile, dict) else None,
        "mode": profile.get("mode") if isinstance(profile, dict) else None,
        "tts_model": source.get("tts_model") if isinstance(source, dict) else None,
        "primary_voice": primary.get("voice_name") if isinstance(primary, dict) else None,
        "questioner_voice": (
            questioner.get("voice_name") if isinstance(questioner, dict) else None
        ),
        "human_approval": (
            source.get("human_approval") if isinstance(source, dict) else None
        ),
    }
    if actual != expected:
        raise RuntimeError("Clean V2 human-approved voice reference mismatch")
    return VOICE_REFERENCE_PROFILE_VERSION


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
    """Fail-loud terminal voice availability failure; never a content block."""

    def __init__(
        self,
        *,
        charon_attempts: int,
        charon_reason: str,
        secondary_reason: str,
        piper_fallback_allowed: bool,
    ) -> None:
        self.charon_attempts = int(charon_attempts)
        self.charon_reason = str(charon_reason or "unknown")
        self.secondary_reason = str(secondary_reason or "unavailable")
        self.piper_fallback_allowed = bool(piper_fallback_allowed)
        super().__init__(
            "CLEAN_V2_VOICE_INFRASTRUCTURE "
            f"reason=charon_unavailable attempts={self.charon_attempts} "
            f"charon_error={self.charon_reason} secondary={self.secondary_reason} "
            f"piper_emergency_enabled={str(self.piper_fallback_allowed).lower()}"
        )


class AzureF0NeuralVoiceSynthesizer:
    """Optional free-tier neural fallback; active only after explicit F0 confirmation."""

    def __init__(
        self,
        api_key: str,
        region: str,
        *,
        free_tier_confirmed: bool,
        voice_approved: bool,
        voice: str = AZURE_F0_VOICE,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.region = str(region or "").strip().lower()
        self.free_tier_confirmed = bool(free_tier_confirmed)
        self.voice_approved = bool(voice_approved)
        self.voice = str(voice or "").strip() or AZURE_F0_VOICE

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_key
            and self.region
            and self.free_tier_confirmed
            and self.voice_approved
        )

    @property
    def unavailable_reason(self) -> str:
        if not self.api_key and not self.region:
            return "azure_f0_not_configured"
        if not self.api_key:
            return "azure_f0_missing_key"
        if not self.region:
            return "azure_f0_missing_region"
        if not self.free_tier_confirmed:
            return "azure_f0_not_confirmed"
        if not self.voice_approved:
            return "azure_f0_voice_not_human_approved"
        return "azure_f0_available"

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        if not self.enabled:
            raise TtsProviderError(self.unavailable_reason)
        if not _AZURE_REGION_RE.fullmatch(self.region):
            raise TtsProviderError("azure_f0_invalid_region")
        if re.search(r"(?m)^\s*[AB]:\s*\S", transcript):
            raise TtsProviderError("azure_f0_dialogue_voice_contract_unsupported")

        escaped = xml_escape(transcript.strip())
        if not escaped:
            raise TtsProviderError("azure_f0_empty_transcript")
        ssml = (
            f'<speak version="1.0" xml:lang="{AZURE_F0_LOCALE}">'
            f'<voice name="{self.voice}">{escaped}</voice></speak>'
        ).encode("utf-8")
        request = urllib.request.Request(
            f"https://{self.region}.tts.speech.microsoft.com/cognitiveservices/v1",
            data=ssml,
            method="POST",
            headers={
                "Content-Type": "application/ssml+xml",
                "Ocp-Apim-Subscription-Key": self.api_key,
                "X-Microsoft-OutputFormat": AZURE_F0_OUTPUT_FORMAT,
                "User-Agent": "Isco-Clean-V2/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                audio = response.read(MAX_TTS_AUDIO_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            raise TtsProviderError(
                f"azure_f0_http_{status}",
                http_status=status,
                retry_after_seconds=_tts_retry_after_seconds(exc),
            ) from None
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise TtsProviderError(
                f"azure_f0_transport_{type(exc).__name__.lower()}"
            ) from None
        if len(audio) > MAX_TTS_AUDIO_BYTES:
            raise TtsProviderError("azure_f0_audio_too_large")
        if len(audio) < 1024 or not audio.startswith(b"RIFF"):
            raise TtsProviderError("azure_f0_invalid_audio")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.azure-f0.tmp")
        temporary.write_bytes(audio)
        try:
            with wave.open(str(temporary), "rb") as wav:
                if (
                    wav.getnchannels() != 1
                    or wav.getsampwidth() != 2
                    or wav.getframerate() != 24000
                    or wav.getnframes() <= 0
                ):
                    raise TtsProviderError("azure_f0_unexpected_wav_format")
            os.replace(temporary, output_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return output_path


class GeminiPrimaryPiperFallbackSynthesizer:
    """Pinned Charon route with bounded retry, optional F0 neural fallback, and opt-in Piper."""

    EXPECTED_PRIMARY_VOICE = "Charon"
    EXPECTED_QUESTIONER_VOICE = "Orus"
    EXPECTED_TTS_MODEL = "gemini-3.1-flash-tts-preview"

    def __init__(
        self,
        api_key: str,
        piper_model_path: Path,
        manifest_path: Path | None = None,
        *,
        tts_model: str = "gemini-3.1-flash-tts-preview",
        azure_api_key: str = "",
        azure_region: str = "",
        azure_free_tier_confirmed: bool = False,
        azure_voice_approved: bool = False,
        allow_piper_fallback: bool = False,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.tts_model = str(tts_model or "").strip() or "gemini-3.1-flash-tts-preview"
        self.piper = PiperVoiceSynthesizer(piper_model_path, manifest_path)
        self.azure = AzureF0NeuralVoiceSynthesizer(
            azure_api_key,
            azure_region,
            free_tier_confirmed=azure_free_tier_confirmed,
            voice_approved=azure_voice_approved,
        )
        self.allow_piper_fallback = bool(allow_piper_fallback)
        self.last_provider: str | None = None
        self.fallback_used: bool | None = None
        self.charon_attempts = 0
        self.voice_roles: dict[str, str] | None = None
        self.voice_approval_status: str | None = None
        self.voice_reference_profile: str | None = None

    def _piper_fallback(self, transcript: str, output_path: Path) -> Path:
        if _spoken_voice_roles(transcript).get("mode") == "dialogue_qa":
            raise VoiceInfrastructureError(
                charon_attempts=self.charon_attempts,
                charon_reason="dialogue_cloud_voice_unavailable",
                secondary_reason="piper_cannot_preserve_two_voice_dialogue",
                piper_fallback_allowed=self.allow_piper_fallback,
            )
        result = self.piper.synthesize(transcript, output_path)
        self.last_provider = f"piper-local:{self.piper.model_path.stem}"
        self.fallback_used = True
        self.voice_approval_status = "emergency_only_not_naturalness_approved"
        self.voice_reference_profile = None
        print(f"Clean V2 voice provider selected: {self.last_provider}")
        return result

    def synthesize(
        self,
        transcript: str,
        output_path: Path,
        *,
        primary_only: bool = False,
    ) -> Path:
        if not transcript.strip():
            raise RuntimeError("cannot synthesize an empty transcript")

        primary_voice, questioner_voice = _legacy_voice_identity()
        if primary_voice != self.EXPECTED_PRIMARY_VOICE:
            raise RuntimeError(
                "Clean V2 primary voice identity mismatch: "
                f"expected={self.EXPECTED_PRIMARY_VOICE} actual={primary_voice}"
            )
        if questioner_voice != self.EXPECTED_QUESTIONER_VOICE:
            raise RuntimeError(
                "Clean V2 questioner voice identity mismatch: "
                f"expected={self.EXPECTED_QUESTIONER_VOICE} actual={questioner_voice}"
            )
        self.voice_reference_profile = _assert_human_approved_voice_reference(
            tts_model=self.tts_model,
            primary_voice=primary_voice,
            questioner_voice=questioner_voice,
        )
        self.voice_roles = _spoken_voice_roles(transcript)
        print(
            "Clean V2 voice role map: "
            + " ".join(f"{key}={value}" for key, value in self.voice_roles.items())
        )

        self.last_provider = None
        self.fallback_used = None
        self.voice_approval_status = None
        self.charon_attempts = 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        charon_error: BaseException | None = None

        if self.api_key:
            for attempt in range(1, CHARON_MAX_ATTEMPTS + 1):
                self.charon_attempts = attempt
                try:
                    _legacy_gemini_synthesize(
                        self.api_key,
                        transcript,
                        output_path,
                        model=self.tts_model,
                        voice=primary_voice,
                        style=SHORT_CHARON_STYLE if primary_only else "",
                    )
                    if not output_path.is_file() or output_path.stat().st_size < 1024:
                        raise RuntimeError("Gemini TTS produced an empty narration file")
                    self.last_provider = f"gemini:{primary_voice}"
                    self.fallback_used = False
                    self.voice_approval_status = "human_approved_reference"
                    print(
                        f"Clean V2 voice provider selected: {self.last_provider} "
                        f"attempt={attempt}/{CHARON_MAX_ATTEMPTS}"
                    )
                    return output_path
                except Exception as exc:
                    charon_error = exc
                    output_path.unlink(missing_ok=True)
                    print(
                        "Clean V2 Charon attempt failed: "
                        f"attempt={attempt}/{CHARON_MAX_ATTEMPTS} "
                        f"error_type={type(exc).__name__} "
                        f"detail={_tts_exception_detail(exc)}"
                    )
                    if attempt >= CHARON_MAX_ATTEMPTS:
                        break
                    delay = _charon_retry_delay(exc, attempt - 1)
                    if delay is None:
                        print(
                            "Clean V2 Charon retry stopped by permanent/long-window evidence: "
                            f"attempt={attempt}/{CHARON_MAX_ATTEMPTS}"
                        )
                        break
                    print(
                        "Clean V2 Charon retry scheduled: "
                        f"next_attempt={attempt + 1}/{CHARON_MAX_ATTEMPTS} "
                        f"delay_seconds={delay:g}"
                    )
                    time.sleep(delay)
        else:
            print("Clean V2 Charon unavailable: missing_api_key")

        charon_reason = _tts_failure_reason(charon_error, missing="missing_api_key")
        if primary_only:
            raise VoiceInfrastructureError(
                charon_attempts=self.charon_attempts,
                charon_reason=charon_reason,
                secondary_reason="primary_only_contract_no_fallback",
                piper_fallback_allowed=False,
            )

        secondary_reason = self.azure.unavailable_reason
        if self.azure.enabled:
            try:
                result = self.azure.synthesize(transcript, output_path)
                self.last_provider = f"azure-f0:{self.azure.voice}"
                self.fallback_used = True
                self.voice_approval_status = "human_approved_fallback"
                self.voice_reference_profile = f"azure-f0:{self.azure.voice}"
                print(f"Clean V2 voice provider selected: {self.last_provider}")
                return result
            except Exception as exc:
                output_path.unlink(missing_ok=True)
                secondary_reason = str(getattr(exc, "reason", type(exc).__name__))[:120]
                print(
                    "Clean V2 Azure F0 neural fallback failed: "
                    f"error_type={type(exc).__name__} "
                    f"detail={_tts_exception_detail(exc)}"
                )
        else:
            print(f"Clean V2 Azure F0 neural fallback unavailable: {secondary_reason}")

        if self.allow_piper_fallback:
            print(
                "Clean V2 emergency Piper fallback explicitly enabled after cloud voice exhaustion"
            )
            try:
                return self._piper_fallback(transcript, output_path)
            except VoiceInfrastructureError:
                raise
            except Exception as exc:
                piper_reason = _tts_failure_reason(
                    exc, missing="piper_unavailable"
                )
                raise VoiceInfrastructureError(
                    charon_attempts=self.charon_attempts,
                    charon_reason=charon_reason,
                    secondary_reason=f"{secondary_reason};piper_{piper_reason}"[:120],
                    piper_fallback_allowed=self.allow_piper_fallback,
                ) from None

        raise VoiceInfrastructureError(
            charon_attempts=self.charon_attempts,
            charon_reason=charon_reason,
            secondary_reason=secondary_reason,
            piper_fallback_allowed=self.allow_piper_fallback,
        )


class GeminiPrimaryNabraFallbackSynthesizer:
    """Production voice route: Charon first, then one local Nabra fallback."""

    EXPECTED_PRIMARY_VOICE = "Charon"
    EXPECTED_QUESTIONER_VOICE = "Orus"

    def __init__(
        self,
        api_key: str,
        *,
        tts_model: str = "gemini-3.1-flash-tts-preview",
        nabra: Any | None = None,
    ) -> None:
        from .nabra_voice import NabraVoiceSynthesizer

        self.api_key = str(api_key or "").strip()
        self.tts_model = str(tts_model or "").strip() or "gemini-3.1-flash-tts-preview"
        self.nabra = nabra if nabra is not None else NabraVoiceSynthesizer()
        self.last_provider: str | None = None
        self.fallback_used: bool | None = None
        self.charon_attempts = 0
        self.voice_roles: dict[str, str] | None = None
        self.voice_approval_status: str | None = None
        self.voice_reference_profile: str | None = None
        # Never mix narrator identities inside one production run. The first
        # successful chunk locks all following chunks to that same route.
        self._route_lock: str | None = None

    def _use_nabra(self, transcript: str, output_path: Path) -> Path:
        if _spoken_voice_roles(transcript).get("mode") == "dialogue_qa":
            raise VoiceInfrastructureError(
                charon_attempts=self.charon_attempts,
                charon_reason="dialogue_charon_unavailable",
                secondary_reason="nabra_single_narrator_only",
                piper_fallback_allowed=False,
            )
        try:
            result = self.nabra.synthesize(transcript, output_path)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise VoiceInfrastructureError(
                charon_attempts=self.charon_attempts,
                charon_reason="charon_unavailable",
                secondary_reason=f"nabra_{_tts_failure_reason(exc, missing='unavailable')}"[:120],
                piper_fallback_allowed=False,
            ) from None
        self._route_lock = "nabra"
        self.last_provider = "nabra:af_msa"
        self.fallback_used = True
        self.voice_approval_status = "human_approved_fallback"
        self.voice_reference_profile = "nabra-82m-v0.1:af_msa:0.87"
        print("Clean V2 voice provider selected: nabra:af_msa")
        return result

    def activate_full_run_nabra_fallback(self) -> None:
        """Lock the next full narration pass to Nabra after a mid-run Charon outage."""
        self._route_lock = "nabra"
        self.last_provider = None
        self.fallback_used = True
        self.voice_approval_status = "human_approved_fallback"
        self.voice_reference_profile = "nabra-82m-v0.1:af_msa:0.87"

    def synthesize(
        self,
        transcript: str,
        output_path: Path,
        *,
        primary_only: bool = False,
    ) -> Path:
        if not transcript.strip():
            raise RuntimeError("cannot synthesize an empty transcript")

        primary_voice, questioner_voice = _legacy_voice_identity()
        if primary_voice != self.EXPECTED_PRIMARY_VOICE:
            raise RuntimeError(
                "Clean V2 primary voice identity mismatch: "
                f"expected={self.EXPECTED_PRIMARY_VOICE} actual={primary_voice}"
            )
        if questioner_voice != self.EXPECTED_QUESTIONER_VOICE:
            raise RuntimeError(
                "Clean V2 questioner voice identity mismatch: "
                f"expected={self.EXPECTED_QUESTIONER_VOICE} actual={questioner_voice}"
            )

        self.voice_roles = _spoken_voice_roles(transcript)
        if self._route_lock == "nabra":
            self.charon_attempts = 0
            return self._use_nabra(transcript, output_path)

        # The first successful Charon chunk locks the production to Charon.
        # A later Charon outage therefore fails closed instead of switching voice
        # mid-video. Nabra is used only when the primary route is unavailable
        # before narrator identity has been established.
        self.last_provider = None
        self.fallback_used = None
        self.voice_approval_status = None
        self.charon_attempts = 0
        output_path.parent.mkdir(parents=True, exist_ok=True)
        charon_error: BaseException | None = None

        if self.api_key:
            for attempt in range(1, CHARON_MAX_ATTEMPTS + 1):
                self.charon_attempts = attempt
                try:
                    _legacy_gemini_synthesize(
                        self.api_key,
                        transcript,
                        output_path,
                        model=self.tts_model,
                        voice=primary_voice,
                        style=SHORT_CHARON_STYLE if primary_only else "",
                    )
                    if not output_path.is_file() or output_path.stat().st_size < 1024:
                        raise RuntimeError("Gemini TTS produced an empty narration file")
                    self._route_lock = "charon"
                    self.last_provider = f"gemini:{primary_voice}"
                    self.fallback_used = False
                    self.voice_approval_status = "human_approved_reference"
                    self.voice_reference_profile = _assert_human_approved_voice_reference(
                        tts_model=self.tts_model,
                        primary_voice=primary_voice,
                        questioner_voice=questioner_voice,
                    )
                    print(
                        f"Clean V2 voice provider selected: {self.last_provider} "
                        f"attempt={attempt}/{CHARON_MAX_ATTEMPTS}"
                    )
                    return output_path
                except Exception as exc:
                    charon_error = exc
                    output_path.unlink(missing_ok=True)
                    print(
                        "Clean V2 Charon attempt failed: "
                        f"attempt={attempt}/{CHARON_MAX_ATTEMPTS} "
                        f"error_type={type(exc).__name__} "
                        f"detail={_tts_exception_detail(exc)}"
                    )
                    if attempt >= CHARON_MAX_ATTEMPTS:
                        break
                    delay = _charon_retry_delay(exc, attempt - 1)
                    if delay is None:
                        break
                    time.sleep(delay)

        charon_reason = _tts_failure_reason(charon_error, missing="missing_api_key")
        if self._route_lock == "charon":
            raise VoiceInfrastructureError(
                charon_attempts=self.charon_attempts,
                charon_reason=charon_reason,
                secondary_reason="narrator_route_locked_to_charon",
                piper_fallback_allowed=False,
            )
        # primary_only is retained as the existing Short performance-style flag.
        # It must not disable the user-approved Nabra backup before a narrator route
        # has been established for this production.
        return self._use_nabra(transcript, output_path)


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
    allowed = ("pexels.com", "pixabay.com")
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


def _stock_local_rank_score(
    *,
    index: int,
    count: int,
    width: int,
    height: int,
    duration: float,
    portrait: bool,
) -> float:
    """Legacy-inspired local ranking over results already returned by one search."""
    count = max(1, int(count))
    relevance = 1.0 - (max(0, int(index)) / count)
    orientation_ok = (height > width) if portrait else (width >= height)
    pixels = min(max(0, width * height), 1920 * 1080) / float(1920 * 1080)
    duration_fit = min(1.0, max(0.0, float(duration)) / 4.0)
    return (
        relevance * 0.55
        + (1.0 if orientation_ok else 0.0) * 0.20
        + pixels * 0.15
        + duration_fit * 0.10
    )


def _short_shot_distribution(total_seconds: float) -> tuple[int, int, int]:
    seconds = max(0.0, float(total_seconds))
    if seconds > SHORT_VISUAL_NINE_SHOT_THRESHOLD_SECONDS:
        return (3, 3, 3)
    if seconds > SHORT_VISUAL_EIGHT_SHOT_THRESHOLD_SECONDS:
        return (3, 2, 3)
    if seconds > SHORT_VISUAL_SEVEN_SHOT_THRESHOLD_SECONDS:
        return (3, 2, 2)
    return (2, 2, 2)


def _expand_short_visual_sequence(
    paths: list[Path],
    section_ids: list[str] | None,
    total_seconds: float,
) -> list[Path]:
    """Create 6-9 rendered shots from at most six already-approved stock assets."""
    if section_ids is None or len(paths) != len(section_ids):
        return list(paths)
    order: list[str] = []
    groups: dict[str, list[Path]] = {}
    for path, section_id in zip(paths, section_ids):
        if section_id not in groups:
            order.append(section_id)
            groups[section_id] = []
        groups[section_id].append(path)
    if len(order) != 3 or any(len(groups[item]) < 2 for item in order):
        return list(paths)

    desired = _short_shot_distribution(total_seconds)
    expanded: list[Path] = []
    for section_id, shot_count in zip(order, desired):
        assets = groups[section_id][:2]
        expanded.extend(assets)
        if shot_count >= 3:
            # Reuse the already-audited first asset as a new local edit beat.
            # _build_section_body_segments gives each occurrence a different
            # motion mode, so this adds a cut without another provider/QA call.
            expanded.append(assets[0])
    return expanded


def _short_motion_filter(
    *,
    width: int,
    height: int,
    seconds: float,
    mode: str,
) -> str:
    """Three restrained deterministic moves; no motion model or extra analysis."""
    frames = max(1, int(round(max(0.5, float(seconds)) * 30.0)))
    if mode == "push":
        z = f"min({1.0 + SHORT_MOTION_ZOOM:.6f},1+{SHORT_MOTION_ZOOM:.6f}*on/{frames})"
        x = "iw/2-(iw/zoom/2)"
    elif mode == "pull":
        z = f"max(1,{1.0 + SHORT_MOTION_ZOOM:.6f}-{SHORT_MOTION_ZOOM:.6f}*on/{frames})"
        x = "iw/2-(iw/zoom/2)"
    else:
        z = f"{1.0 + SHORT_MOTION_ZOOM:.6f}"
        x = f"(iw-iw/zoom)*on/{frames}"
    y = "ih/2-(ih/zoom/2)"
    return (
        f"zoompan=z='{z}':x='{x}':y='{y}':"
        f"d=1:s={width}x{height}:fps=30"
    )


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

        short_ai_still: Path | None = None
        short_ai_still_used = False
        if fmt == "short":
            short_ai_still, still_reason = _validated_local_short_ai_still()
            if still_reason:
                self._event(
                    "local_ai_still",
                    "",
                    "unavailable",
                    wire_attempted=False,
                    reason=still_reason,
                )

        short_shots_by_section: dict[str, int] = {}
        if fmt == "short" and len(sections) == 3:
            # Exactly two provider-backed assets per section at most. Richer
            # 7-9 shot pacing is created later from these same approved assets
            # locally, so provider and Vision load does not scale with shot count.
            short_shots_by_section = {
                str(section.get("id") or ""): 2
                for section in sections
                if isinstance(section, Mapping)
            }

        def _acquire_one(query: str, section_id: str, *, auxiliary: bool) -> bool:
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
                if auxiliary:
                    candidate["pacing_auxiliary"] = True
                clips.append(destination)
                rights.append(candidate)
                return True
            return False

        def _acquire_local_ai_still(section_id: str) -> bool:
            nonlocal short_ai_still_used
            if short_ai_still is None or short_ai_still_used:
                return False
            destination = output_dir / f"visual-{len(clips) + 1:02d}.mp4"
            try:
                _render_local_short_ai_still(short_ai_still, destination)
                if self.media_preflight is not None:
                    blocked = self.media_preflight(destination)
                    if blocked is not None:
                        destination.unlink(missing_ok=True)
                        self._event(
                            "local_ai_still",
                            "",
                            "security_blocked",
                            wire_attempted=False,
                            reason=str(blocked.get("local_media_rejection") or "security_v1_block")[:80],
                        )
                        return False
                color_ok, color_reason = _short_visual_color_compatible(destination)
                if not color_ok:
                    destination.unlink(missing_ok=True)
                    self._event(
                        "local_ai_still",
                        "",
                        "color_rejected",
                        wire_attempted=False,
                        reason=color_reason,
                    )
                    return False
                if self.media_transform is not None:
                    destination = Path(self.media_transform(destination))
            except Exception as exc:
                destination.unlink(missing_ok=True)
                self._event(
                    "local_ai_still",
                    "",
                    "failed",
                    wire_attempted=False,
                    reason=str(exc)[:80],
                )
                return False

            short_ai_still_used = True
            clips.append(destination)
            rights.append(
                {
                    "provider": "generated_local_ai_still",
                    "asset_id": _sha256(short_ai_still)[:16],
                    "source_url": None,
                    "creator": "local user-provided AI still",
                    "creator_url": None,
                    "query": None,
                    "local_file": destination.name,
                    "section_id": section_id,
                    "pacing_auxiliary": True,
                    "generation_cost": 0,
                    "network_generation_calls": 0,
                }
            )
            self._event(
                "local_ai_still",
                "",
                "selected",
                wire_attempted=False,
                reason="zero_cost_local_insert",
            )
            return True

        for section in sections:
            query = str(section.get("visual_query_en") or "").strip()
            alt_query = str(section.get("visual_query_alt_en") or "").strip()
            if not query:
                continue
            if self.query_normalizer is not None:
                query = self.query_normalizer(query)
                if alt_query:
                    alt_query = self.query_normalizer(alt_query)
            section_id = str(section.get("id") or "")
            if not _acquire_one(query, section_id, auxiliary=False):
                continue

            section_seconds = (
                section_estimated_seconds.get(section_id)
                if section_estimated_seconds is not None
                else None
            )
            if fmt == "short":
                # Two intents come from the same Planning call and produce at
                # most two provider-backed assets for this section. Any third
                # rendered beat is a local edit reuse, never another search/QA call.
                shots = short_shots_by_section.get(section_id, 1)
                extra_queries = [alt_query or query]
                for extra_index in range(max(0, shots - 1)):
                    if section_id == "s2" and extra_index == 0 and _acquire_local_ai_still(section_id):
                        continue
                    selected_query = extra_queries[extra_index % len(extra_queries)]
                    if not _acquire_one(selected_query, section_id, auxiliary=True):
                        break
            elif section_seconds is not None and section_seconds > PACING_MAX_SHOT_SECONDS:
                # Longer formats retain the existing narration-weighted pacing
                # expansion and its original 3.5s / max-3 bounds.
                shots = min(
                    PACING_MAX_SHOTS_PER_SECTION,
                    math.ceil(section_seconds / PACING_MAX_SHOT_SECONDS),
                )
                while shots > 1 and (section_seconds / shots) < PACING_MIN_SHOT_SECONDS:
                    shots -= 1
                for _ in range(shots - 1):
                    if not _acquire_one(query, section_id, auxiliary=True):
                        break

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

    estimated = _section_estimated_seconds_from_manifest(output_dir)
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
COLOR_MATCH_STRENGTH = 0.55
COLOR_MATCH_SCALE_MIN = 0.88
COLOR_MATCH_SCALE_MAX = 1.12
COLOR_MATCH_OFFSET_MAX = 18.0
MASTER_LOOK_LUT_SIZE = 17
MASTER_LOOK_CONTRAST = 1.045
MASTER_LOOK_SATURATION = 0.98
MASTER_LOOK_WARM_R = 0.012
MASTER_LOOK_WARM_G = 0.004
MASTER_LOOK_WARM_B = -0.010

# Final deterministic cinematic finish shared by Film and Short.
# It cannot invent missing scene geometry; it deepens the footage that was
# actually selected: stronger local separation, crisper edges and a restrained
# optical falloff without adding AI/network cost or changing timing.
CINEMATIC_FINISH_VERSION = "clean-v2-cinematic-finish-v1"
CINEMATIC_FINISH_FILTER = (
    "eq=contrast=1.050:brightness=-0.008:saturation=1.030:gamma=0.990,"
    "unsharp=5:5:0.45:5:5:0.0,"
    "vignette=PI/12"
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
    """Choose the medoid-like real clip nearest the episode's median color stats."""
    if not measured:
        raise ValueError("reference selection requires measured clips")
    rows = list(measured.items())
    medians = (
        statistics.median(item.mean_r for _, item in rows),
        statistics.median(item.mean_g for _, item in rows),
        statistics.median(item.mean_b for _, item in rows),
        statistics.median(item.std_r for _, item in rows),
        statistics.median(item.std_g for _, item in rows),
        statistics.median(item.std_b for _, item in rows),
    )

    def distance(stats: _RgbStats) -> float:
        means = (stats.mean_r, stats.mean_g, stats.mean_b)
        stds = (stats.std_r, stats.std_g, stats.std_b)
        mean_distance = sum((value - target) ** 2 for value, target in zip(means, medians[:3]))
        std_distance = sum((value - target) ** 2 for value, target in zip(stds, medians[3:]))
        return mean_distance + (0.25 * std_distance)

    return min(rows, key=lambda row: distance(row[1]))[0]


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
        "method": "bounded_rgb_mean_std_reference_match_v1",
        "match_strength": COLOR_MATCH_STRENGTH,
        "master_look": "warm_neutral_cube_v1",
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
    """One restrained warm-neutral look shared by every final frame."""
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
        'TITLE "Isco Warm Neutral Master v1"',
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
    motion_mode: str | None = None,
    grade_filter: str | None = None,
) -> Path:
    grade = _grade_clip_filter(source) if grade_filter is None else grade_filter
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps=30"
    if motion_mode:
        vf = f"{vf}," + _short_motion_filter(
            width=width,
            height=height,
            seconds=seconds,
            mode=motion_mode,
        )
    if grade:
        vf = f"{vf},{grade}"
    vf = f"{vf},trim=duration={seconds:.3f},setpts=PTS-STARTPTS"
    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-stream_loop",
            "-1",
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
    short_motion_lite: bool = False,
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
                    motion_mode=(
                        ("push", "pan", "pull")[clip_index % 3]
                        if short_motion_lite
                        else None
                    ),
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
    if fmt in {"short", "film"} and timeline_path.is_file():
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
        # Short may render 6-9 edit beats while keeping at most six provider-backed
        # assets. Extra beats reuse already-audited assets locally.
        paths = [Path(item) for item in visual_paths]
        if fmt == "short":
            section_ids = _pacing_section_ids(Path(output_path).parent, paths)
            paths = _expand_short_visual_sequence(paths, section_ids, duration)
        durations = _section_slot_durations(Path(output_path).parent, paths, duration)

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
                short_motion_lite=(fmt == "short"),
                grade_filters=grade_filters,
            )
        else:
            body_segments = []

        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        for path in opening_paths_for_render:
            command.extend(["-stream_loop", "-1", "-i", str(path)])
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
        master_lut = _write_master_look_lut(work_dir / "warm-neutral-master-v1.cube")
        filters.append(f"{''.join(labels)}concat=n={input_index}:v=1:a=0[vcat]")
        master_look = (
            f"lut3d=file='{_ffmpeg_filter_path(master_lut)}':interp=tetrahedral,"
            f"{CINEMATIC_FINISH_FILTER}"
        )
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
