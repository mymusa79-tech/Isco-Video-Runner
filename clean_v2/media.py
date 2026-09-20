from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


MAX_MEDIA_BYTES = 160 * 1024 * 1024
MAX_SEARCH_RESPONSE_BYTES = 8 * 1024 * 1024

# Visual-pacing bounds for splitting one section's flat render slot across
# several distinct same-query clips instead of one clip lingering for the
# whole slot. Numeric philosophy borrowed from the legacy Engine's M7
# adaptive pacing (_MIN_ADAPTIVE_SHOT_SECONDS / MAX_SHOTS_PER_SCENE), not its
# semantic-director machinery - Clean V2 has no beat/scene/candidate pipeline
# to drive that, so this is the plain arithmetic equivalent.
PACING_MAX_SHOT_SECONDS = 22.0
PACING_MIN_SHOT_SECONDS = 3.5
PACING_MAX_SHOTS_PER_SECTION = 3


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
) -> Path:
    """Reuse the legacy Gemini TTS implementation with exactly one provider attempt."""
    from isco_video_agent.providers.gemini import synthesize_wav

    return synthesize_wav(
        api_key,
        transcript,
        output_path,
        model=model,
        voice=voice,
        style="",
        attempts=1,
    )


class GeminiPrimaryPiperFallbackSynthesizer:
    """Clean V2 voice route: approved Gemini identity first, verified Piper second."""

    EXPECTED_PRIMARY_VOICE = "Charon"

    def __init__(
        self,
        api_key: str,
        piper_model_path: Path,
        manifest_path: Path | None = None,
        *,
        tts_model: str = "gemini-3.1-flash-tts-preview",
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.tts_model = str(tts_model or "").strip() or "gemini-3.1-flash-tts-preview"
        self.piper = PiperVoiceSynthesizer(piper_model_path, manifest_path)
        self.last_provider: str | None = None
        self.fallback_used: bool | None = None

    def _fallback(self, transcript: str, output_path: Path) -> Path:
        result = self.piper.synthesize(transcript, output_path)
        self.last_provider = f"piper-local:{self.piper.model_path.stem}"
        self.fallback_used = True
        print(f"Clean V2 voice provider selected: {self.last_provider}")
        return result

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        if not transcript.strip():
            raise RuntimeError("cannot synthesize an empty transcript")

        primary_voice, _ = _legacy_voice_identity()
        if primary_voice != self.EXPECTED_PRIMARY_VOICE:
            raise RuntimeError(
                "Clean V2 primary voice identity mismatch: "
                f"expected={self.EXPECTED_PRIMARY_VOICE} actual={primary_voice}"
            )

        if not self.api_key:
            print("Clean V2 Gemini TTS unavailable: missing_api_key; using Piper fallback")
            return self._fallback(transcript, output_path)

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _legacy_gemini_synthesize(
                self.api_key,
                transcript,
                output_path,
                model=self.tts_model,
                voice=primary_voice,
            )
            if not output_path.is_file() or output_path.stat().st_size < 1024:
                raise RuntimeError("Gemini TTS produced an empty narration file")
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            print(
                "Clean V2 Gemini TTS failed; using Piper fallback: "
                f"error_type={type(exc).__name__}"
            )
            return self._fallback(transcript, output_path)

        self.last_provider = f"gemini:{primary_voice}"
        self.fallback_used = False
        print(f"Clean V2 voice provider selected: {self.last_provider}")
        return output_path


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
            for video in body.get("videos") or []:
                if not isinstance(video, dict):
                    continue
                identity = ("pexels", str(video.get("id") or ""))
                selected = _pexels_file(video, portrait=portrait)
                if identity in self._used or selected is None:
                    continue
                self._used.add(identity)
                self._event("pexels", query, "selected", wire_attempted=True)
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
            for hit in body.get("hits") or []:
                if not isinstance(hit, dict):
                    continue
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
                oriented = [
                    item
                    for item in candidates
                    if ((int(item.get("height") or 0) > int(item.get("width") or 0)) if portrait else (int(item.get("width") or 0) >= int(item.get("height") or 0)))
                ]
                selected = (oriented or candidates)[0]
                self._used.add(identity)
                self._event("pixabay", query, "selected", wire_attempted=True)
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
        section_flat_slot_seconds: float | None = None,
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        portrait = fmt in {"moment", "story"}
        clips: list[Path] = []
        rights: list[dict[str, Any]] = []
        sections = list(plan.get("sections") or [])[: max(1, int(max_visuals))]

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

        for section in sections:
            query = str(section.get("visual_query_en") or "").strip()
            if not query:
                continue
            if self.query_normalizer is not None:
                query = self.query_normalizer(query)
            section_id = str(section.get("id") or "")
            if not _acquire_one(query, section_id, auxiliary=False):
                continue

            # A section whose flat render slot would leave a single clip on
            # screen too long gets extra same-query coverage instead: same
            # stock search, no new AI call, bounded by the pacing constants
            # above so this never fires unbounded provider requests.
            if (
                section_flat_slot_seconds is not None
                and section_flat_slot_seconds > PACING_MAX_SHOT_SECONDS
            ):
                shots = min(
                    PACING_MAX_SHOTS_PER_SECTION,
                    math.ceil(section_flat_slot_seconds / PACING_MAX_SHOT_SECONDS),
                )
                while shots > 1 and (section_flat_slot_seconds / shots) < PACING_MIN_SHOT_SECONDS:
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
        portrait = fmt in {"moment", "story"}
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
        portrait = fmt in {"moment", "story"}
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


def _pacing_section_ids(output_dir: Path, paths: list[Path]) -> list[str] | None:
    """Map each body clip to its section_id via rights-manifest.json.

    Returns None (never partially) when the manifest is missing or does not
    cover every path, so callers fall back to the plain uniform-slot split
    exactly as before - this is read-only evidence lookup, never a hard
    requirement.
    """
    manifest_path = output_dir / "rights-manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    assets = payload.get("assets") if isinstance(payload, dict) else None
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


def _section_slot_durations(
    output_dir: Path, paths: list[Path], total_seconds: float, *, pad: float = 0.12
) -> list[float]:
    """Split total_seconds across paths, keeping each section's original flat
    share intact and only subdividing it among that section's own clips.

    A section that acquired extra same-query clips (visual pacing for a long
    section) shares its one flat slot across those clips instead of shrinking
    every other section's share just because the clip count grew.
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
    flat_slot = total_seconds / max(1, len(order))
    return [(flat_slot / counts[section_id]) + pad for section_id in section_ids]


def render_video(
    narration_path: Path,
    visual_paths: list[Path],
    output_path: Path,
    fmt: str,
) -> Path:
    if not visual_paths:
        raise RuntimeError("render requires at least one visual")
    duration = probe_duration(narration_path)
    portrait = fmt in {"moment", "story"}
    width, height = ((1080, 1920) if portrait else (1920, 1080))

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
        # No fixed upper bound here either, for the same reason as the body
        # clips above: acquire() already bounds the real total via
        # max_visuals * PACING_MAX_SHOTS_PER_SECTION.
        paths = [Path(item) for item in visual_paths]
        durations = _section_slot_durations(Path(output_path).parent, paths, duration)

    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for path in paths:
        command.extend(["-stream_loop", "-1", "-i", str(path)])
    command.extend(["-i", str(narration_path)])
    filters: list[str] = []
    labels: list[str] = []
    for index, clip_seconds in enumerate(durations):
        label = f"v{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,trim=duration={clip_seconds:.3f},"
            f"setpts=PTS-STARTPTS[{label}]"
        )
    filters.append(
        f"{''.join(labels)}concat=n={len(paths)}:v=1:a=0[vout]"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            f"{len(paths)}:a:0",
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
            "-shortest",
            "-y",
            str(output_path),
        ]
    )
    _run(command, timeout=1800)
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
    video_streams = sum(1 for item in streams if item.get("codec_type") == "video")
    audio_streams = sum(1 for item in streams if item.get("codec_type") == "audio")
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
        "video_streams": video_streams,
        "audio_streams": audio_streams,
        "quality_layers_executed": [],
    }
