from __future__ import annotations

import hashlib
import json
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
from typing import Any, Mapping


MAX_MEDIA_BYTES = 160 * 1024 * 1024
MAX_SEARCH_RESPONSE_BYTES = 8 * 1024 * 1024


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
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._used: set[tuple[str, str]] = set()

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
    ) -> tuple[list[Path], list[dict[str, Any]]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        portrait = fmt in {"moment", "story"}
        clips: list[Path] = []
        rights: list[dict[str, Any]] = []
        sections = list(plan.get("sections") or [])[: max(1, int(max_visuals))]
        for section in sections:
            query = str(section.get("visual_query_en") or "").strip()
            if not query:
                continue
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
                candidate = {
                    key: value
                    for key, value in candidate.items()
                    if key != "download_url"
                }
                candidate["local_file"] = destination.name
                clips.append(destination)
                rights.append(candidate)
                break

        if not clips:
            fallback = output_dir / "visual-fallback.mp4"
            create_fallback_visual(fallback, portrait=portrait)
            clips.append(fallback)
            rights.append(
                {
                    "provider": "generated_local",
                    "asset_id": "clean-v2-fallback",
                    "source_url": None,
                    "creator": "Isco Clean V2",
                    "creator_url": None,
                    "query": None,
                    "local_file": fallback.name,
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
    paths = visual_paths[:5]
    slot = (duration / len(paths)) + 0.12
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    for path in paths:
        command.extend(["-stream_loop", "-1", "-i", str(path)])
    command.extend(["-i", str(narration_path)])
    filters: list[str] = []
    labels: list[str] = []
    for index in range(len(paths)):
        label = f"v{index}"
        labels.append(f"[{label}]")
        filters.append(
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1,fps=30,trim=duration={slot:.3f},"
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
