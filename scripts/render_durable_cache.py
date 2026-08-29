from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.security import secret_free_subprocess_env

from scripts import m9_live_binding


CACHE_SCHEMA_VERSION = 2
CACHE_NAMESPACE = "render-durable-v2"
MAX_FILE_BYTES = 1536 * 1024 * 1024
MAX_TOTAL_BYTES = 3 * 1024 * 1024 * 1024
MAX_ENTRIES_BY_KIND = {
    "m9-pair": 24,
    "burn": 4,
    "final": 2,
}
_KINDS = tuple(MAX_ENTRIES_BY_KIND)

_INSTALLED = False
_ORIGINAL_PRODUCE: Callable[..., Any] | None = None
_ORIGINAL_BURN: Callable[..., Any] | None = None
_ORIGINAL_MUX: Callable[..., Any] | None = None
_ORIGINAL_M9_PAIR: Callable[..., Any] | None = None
_PENDING_FINAL: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "isco_render_pending_final",
    default=None,
)
_FFMPEG_IDENTITY: dict[str, str] | None = None
_FONT_IDENTITY: dict[str, str] | None = None


def _shared_root() -> Path | None:
    explicit = (os.environ.get("ISCO_RENDER_CACHE_PATH") or "").strip()
    if explicit:
        return Path(explicit)
    raw = (os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip()
    return Path(raw) / "render" if raw else None


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"render_cache_missing_contract_env:{name}")
    return value


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _module_sha(module: Any) -> str:
    module_file = getattr(module, "__file__", None)
    if not module_file:
        return "unknown"
    return _sha256_file(Path(module_file).resolve())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _binding_hash(binding: dict[str, Any]) -> str:
    return _sha256_bytes(_json_bytes(binding))


def _callable_identity(fn: Callable[..., Any]) -> dict[str, str]:
    module_name = str(getattr(fn, "__module__", "") or "")
    module = sys.modules.get(module_name)
    return {
        "module": module_name,
        "module_sha256": _module_sha(module) if module is not None else "unknown",
        "qualname": str(getattr(fn, "__qualname__", getattr(fn, "__name__", "unknown"))),
    }


def _regular_file_binding(path: Path) -> dict[str, Any] | None:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    size = path.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        return None
    return {
        "sha256": _sha256_file(path),
        "byte_length": size,
        "suffix": path.suffix.lower(),
    }


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError("non_finite_float")
        return value
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise TypeError("non_string_dict_key")
            result[key] = _canonical_value(value[key])
        return result
    raise TypeError(f"unsupported_semantic_value:{type(value).__name__}")


def _canonical_kwargs(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return {key: _canonical_value(kwargs[key]) for key in sorted(kwargs)}
    except (TypeError, ValueError):
        return None


def _ffmpeg_identity() -> dict[str, str] | None:
    global _FFMPEG_IDENTITY
    if _FFMPEG_IDENTITY is not None:
        return dict(_FFMPEG_IDENTITY)
    try:
        output = subprocess.check_output(
            ["ffmpeg", "-version"],
            env=secret_free_subprocess_env(),
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None
    output = output.strip()
    if not output:
        return None
    first = output.splitlines()[0].strip()
    _FFMPEG_IDENTITY = {
        "first_line": first,
        "sha256": _sha256_bytes(output.encode("utf-8")),
    }
    return dict(_FFMPEG_IDENTITY)


def _font_identity() -> dict[str, str] | None:
    global _FONT_IDENTITY
    if _FONT_IDENTITY is not None:
        return dict(_FONT_IDENTITY)
    try:
        output = subprocess.check_output(
            ["fc-match", "-f", "%{file}\n", "Noto Sans Arabic"],
            env=secret_free_subprocess_env(),
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None
    candidates = [Path(line.strip()) for line in output.splitlines() if line.strip()]
    font = next((path for path in candidates if path.is_file() and not path.is_symlink()), None)
    if font is None:
        return None
    _FONT_IDENTITY = {
        "filename": font.name,
        "sha256": _sha256_file(font),
    }
    return dict(_FONT_IDENTITY)


def _base_binding(kind: str, *, ffmpeg: dict[str, str]) -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "ffmpeg": ffmpeg,
    }


def _m9_pair_binding(
    left: Path,
    right: Path,
    renderer: Callable[..., Any],
    *,
    dissolve_seconds: float,
) -> dict[str, Any] | None:
    left_binding = _regular_file_binding(left)
    right_binding = _regular_file_binding(right)
    ffmpeg = _ffmpeg_identity()
    if left_binding is None or right_binding is None or ffmpeg is None:
        return None
    return {
        **_base_binding("m9-pair", ffmpeg=ffmpeg),
        "renderer": _callable_identity(renderer),
        "m9_module_sha256": _module_sha(m9_live_binding),
        "left": left_binding,
        "right": right_binding,
        "dissolve_millis": int(round(float(dissolve_seconds) * 1000.0)),
    }


def _burn_binding(
    video: Path,
    srt: Path,
    renderer: Callable[..., Any],
    *,
    portrait: bool,
) -> dict[str, Any] | None:
    video_binding = _regular_file_binding(video)
    srt_binding = _regular_file_binding(srt)
    ffmpeg = _ffmpeg_identity()
    font = _font_identity()
    if video_binding is None or srt_binding is None or ffmpeg is None or font is None:
        return None
    return {
        **_base_binding("burn", ffmpeg=ffmpeg),
        "renderer": _callable_identity(renderer),
        "video": video_binding,
        "srt": srt_binding,
        "portrait": bool(portrait),
        "font": font,
    }


def _final_binding(
    video: Path,
    narration: Path | None,
    renderer: Callable[..., Any],
    music: Path | None,
    kwargs: dict[str, Any],
) -> dict[str, Any] | None:
    video_binding = _regular_file_binding(video)
    ffmpeg = _ffmpeg_identity()
    canonical_kwargs = _canonical_kwargs(kwargs)
    if video_binding is None or ffmpeg is None or canonical_kwargs is None:
        return None

    narration_binding = None
    if narration is not None:
        narration_binding = _regular_file_binding(narration)
        if narration_binding is None:
            return None

    music_binding = None
    if music is not None:
        music_binding = _regular_file_binding(music)
        if music_binding is None:
            return None

    return {
        **_base_binding("final", ffmpeg=ffmpeg),
        "renderer": _callable_identity(renderer),
        "video": video_binding,
        "narration": narration_binding,
        "music": music_binding,
        "kwargs": canonical_kwargs,
    }


def _entry(root: Path, kind: str, fingerprint: str) -> Path:
    return Path(root) / kind / fingerprint


def _atomic_copy(source: Path, dest: Path) -> None:
    source = Path(source)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        shutil.copyfile(source, tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _validate_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any] | None = None,
):
    entry = _entry(root, kind, fingerprint)
    manifest = _load_json(entry / "manifest.json")
    artifact = entry / "artifact.mp4"
    if manifest is None or entry.is_symlink() or not entry.is_dir() or artifact.is_symlink() or not artifact.is_file():
        return None
    stored_binding = manifest.get("binding")
    if (
        manifest.get("schema_version") != CACHE_SCHEMA_VERSION
        or manifest.get("kind") != kind
        or not isinstance(stored_binding, dict)
        or stored_binding.get("namespace") != CACHE_NAMESPACE
        or stored_binding.get("schema_version") != CACHE_SCHEMA_VERSION
        or manifest.get("fingerprint") != _binding_hash(stored_binding)
        or manifest.get("fingerprint") != fingerprint
    ):
        return None
    if binding is not None and stored_binding != binding:
        return None
    size = artifact.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES or manifest.get("byte_length") != size:
        return None
    sha = str(manifest.get("sha256") or "")
    if not sha or _sha256_file(artifact) != sha:
        return None
    return artifact, manifest


def _persist_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any],
    source: Path,
) -> None:
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        return
    size = source.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES:
        return
    entry = _entry(root, kind, fingerprint)
    artifact = entry / "artifact.mp4"
    _atomic_copy(source, artifact)
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "fingerprint": fingerprint,
        "binding": binding,
        "sha256": _sha256_file(artifact),
        "byte_length": artifact.stat().st_size,
    }
    _atomic_json(entry / "manifest.json", manifest)


def _restore_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any],
    dest: Path,
) -> bool:
    valid = _validate_entry(root, kind, fingerprint, binding)
    if valid is None:
        return False
    artifact, _manifest = valid
    dest = Path(dest)
    try:
        _atomic_copy(artifact, dest)
        print(f"Render durable {kind} HIT fingerprint={fingerprint[:12]}")
        return True
    except Exception as exc:
        dest.unlink(missing_ok=True)
        print(f"Render durable {kind} restore rejected ({type(exc).__name__})")
        return False


def _wrap_m9_pair(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(left: Path, right: Path, dest: Path, *, dissolve_seconds: float = 0.36) -> Path:
        left = Path(left)
        right = Path(right)
        dest = Path(dest)
        binding = _m9_pair_binding(
            left,
            right,
            original,
            dissolve_seconds=dissolve_seconds,
        )
        if binding is None:
            return Path(original(left, right, dest, dissolve_seconds=dissolve_seconds))
        fingerprint = _binding_hash(binding)
        if _restore_entry(root, "m9-pair", fingerprint, binding, dest):
            return dest
        result = Path(original(left, right, dest, dissolve_seconds=dissolve_seconds))
        try:
            _persist_entry(root, "m9-pair", fingerprint, binding, result)
        except Exception as exc:
            print(f"Render durable m9-pair persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_render_durable_m9_pair = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _wrap_burn(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(video: Path, srt: Path, output: Path, *, portrait: bool = False) -> Path:
        video = Path(video)
        srt = Path(srt)
        output = Path(output)
        if output.name != "picture-text.mp4":
            return Path(original(video, srt, output, portrait=portrait))
        binding = _burn_binding(video, srt, original, portrait=portrait)
        if binding is None:
            return Path(original(video, srt, output, portrait=portrait))
        fingerprint = _binding_hash(binding)
        if _restore_entry(root, "burn", fingerprint, binding, output):
            return output
        result = Path(original(video, srt, output, portrait=portrait))
        try:
            _persist_entry(root, "burn", fingerprint, binding, result)
        except Exception as exc:
            print(f"Render durable burn persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_render_durable_burn = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _wrap_mux(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(video, narration, output, music=None, **kwargs):
        output = Path(output)
        video_path = Path(video)
        narration_path = Path(narration) if narration is not None else None
        music_path = Path(music) if music is not None else None
        if output.name != "final.mp4":
            return Path(original(video_path, narration, output, music=music, **kwargs))

        binding = _final_binding(video_path, narration_path, original, music_path, kwargs)
        if binding is None:
            return Path(original(video_path, narration, output, music=music, **kwargs))

        # A stale quality document must never certify a restored artifact. The Engine
        # writes quality-final.json only after probing this exact mux result.
        (output.parent / "quality-final.json").unlink(missing_ok=True)

        fingerprint = _binding_hash(binding)
        hit = _restore_entry(root, "final", fingerprint, binding, output)
        pending = _PENDING_FINAL.get()
        if hit:
            if pending is not None:
                pending.append(
                    {
                        "root": output.parent,
                        "dest": output,
                        "fingerprint": fingerprint,
                        "binding": binding,
                        "hit": True,
                    }
                )
            return output

        result = Path(original(video_path, narration, output, music=music, **kwargs))
        if pending is not None:
            pending.append(
                {
                    "root": output.parent,
                    "dest": result,
                    "fingerprint": fingerprint,
                    "binding": binding,
                    "hit": False,
                }
            )
        return result

    wrapped._isco_render_durable_mux = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _quality_passed(output_root: Path) -> bool:
    quality = _load_json(Path(output_root) / "quality-final.json")
    if quality is None:
        return False
    if not (
        quality.get("duration_ok") is True
        and quality.get("audio_ok") is True
        and quality.get("av_sync_ok") is True
        and int(quality.get("video_streams") or 0) >= 1
    ):
        return False
    if str(quality.get("format") or "").strip().lower() != "moment":
        return int(quality.get("audio_streams") or 0) >= 1
    return True


def _reconcile_final_candidates(cache_root: Path, pending: list[dict[str, Any]]) -> None:
    for item in pending:
        output_root = Path(item["root"])
        dest = Path(item["dest"])
        fingerprint = str(item["fingerprint"])
        binding = item["binding"]
        hit = bool(item["hit"])
        passed = _quality_passed(output_root)
        if passed:
            if not hit:
                try:
                    _persist_entry(cache_root, "final", fingerprint, binding, dest)
                    print(f"Render durable final PROMOTED after Engine QC fingerprint={fingerprint[:12]}")
                except Exception as exc:
                    print(f"Render durable final promotion skipped ({type(exc).__name__})")
            continue
        if hit:
            shutil.rmtree(_entry(cache_root, "final", fingerprint), ignore_errors=True)
            print(f"Render durable final EVICTED after current Engine QC rejection fingerprint={fingerprint[:12]}")


@contextlib.contextmanager
def _final_candidate_scope() -> Iterator[list[dict[str, Any]]]:
    pending: list[dict[str, Any]] = []
    token = _PENDING_FINAL.set(pending)
    try:
        yield pending
    finally:
        _PENDING_FINAL.reset(token)


def install_render_durable_cache() -> None:
    """Cache only deterministic FFmpeg work while keeping every cinematic wrapper live.

    Installation belongs after M9/M10/CTA produce wrappers but before Narrative Music
    Dynamics wraps the global mux. M9 continues to execute and only its expensive xfade
    pair renderer is cached; SFX/M10/CTA/Narrative Music all execute on every final hit.
    Audio Semantic Integrity remains an outer runtime authority and current Engine QC
    probes every restored final before it can remain durable.
    """
    global _INSTALLED, _ORIGINAL_PRODUCE, _ORIGINAL_BURN, _ORIGINAL_MUX, _ORIGINAL_M9_PAIR
    if _INSTALLED:
        return
    root = _shared_root()
    if root is None:
        print("Render durable cache disabled: durable stage cache not configured")
        return
    if getattr(orchestrator.mux, "_isco_narrative_music_dynamics", False):
        raise RuntimeError("render_cache_install_order_must_precede_narrative_music_dynamics")
    root.mkdir(parents=True, exist_ok=True)

    _ORIGINAL_M9_PAIR = m9_live_binding._render_pair
    m9_live_binding._render_pair = _wrap_m9_pair(root, _ORIGINAL_M9_PAIR)

    _ORIGINAL_BURN = orchestrator.burn_srt
    orchestrator.burn_srt = _wrap_burn(root, _ORIGINAL_BURN)

    _ORIGINAL_MUX = orchestrator.mux
    orchestrator.mux = _wrap_mux(root, _ORIGINAL_MUX)

    _ORIGINAL_PRODUCE = orchestrator.produce
    current = _ORIGINAL_PRODUCE

    def wrapped_produce(*args, **kwargs):
        with _final_candidate_scope() as pending:
            try:
                return current(*args, **kwargs)
            finally:
                _reconcile_final_candidates(root, pending)

    wrapped_produce._isco_render_durable_produce = True
    wrapped_produce._isco_render_durable_original = current
    orchestrator.produce = wrapped_produce
    _INSTALLED = True
    print(
        "Render durable cache installed: M9 dissolve segments + subtitle burn + inner Engine mux; "
        "cinematic wrappers and quality gates remain live"
    )


def reset_render_durable_cache_for_tests() -> None:
    global _INSTALLED, _ORIGINAL_PRODUCE, _ORIGINAL_BURN, _ORIGINAL_MUX, _ORIGINAL_M9_PAIR
    if _ORIGINAL_PRODUCE is not None and getattr(orchestrator.produce, "_isco_render_durable_produce", False):
        orchestrator.produce = _ORIGINAL_PRODUCE
    if _ORIGINAL_BURN is not None and getattr(orchestrator.burn_srt, "_isco_render_durable_burn", False):
        orchestrator.burn_srt = _ORIGINAL_BURN
    if _ORIGINAL_MUX is not None and getattr(orchestrator.mux, "_isco_render_durable_mux", False):
        orchestrator.mux = _ORIGINAL_MUX
    if _ORIGINAL_M9_PAIR is not None and getattr(m9_live_binding._render_pair, "_isco_render_durable_m9_pair", False):
        m9_live_binding._render_pair = _ORIGINAL_M9_PAIR
    _ORIGINAL_PRODUCE = None
    _ORIGINAL_BURN = None
    _ORIGINAL_MUX = None
    _ORIGINAL_M9_PAIR = None
    _INSTALLED = False


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_cache_for_persistence(shared_root: Path) -> bool:
    """Validate and cap Render only; TTS/Media semantic namespaces remain independent."""
    root = Path(shared_root) / "render"
    if not root.exists():
        return False
    if root.is_symlink() or not root.is_dir():
        _remove_path(root)
        return False

    valid: list[tuple[float, Path, int, str]] = []
    for kind in _KINDS:
        kind_root = root / kind
        if not kind_root.exists():
            continue
        if kind_root.is_symlink() or not kind_root.is_dir():
            _remove_path(kind_root)
            continue
        kind_valid: list[tuple[float, Path, int, str]] = []
        for entry in list(kind_root.iterdir()):
            manifest = _load_json(entry / "manifest.json") if entry.is_dir() and not entry.is_symlink() else None
            fingerprint = str(manifest.get("fingerprint") or "") if manifest else ""
            checked = _validate_entry(root, kind, fingerprint) if fingerprint else None
            if checked is None:
                _remove_path(entry)
                continue
            artifact, _manifest = checked
            item = (entry.stat().st_mtime, entry, artifact.stat().st_size, kind)
            kind_valid.append(item)
        cap = int(MAX_ENTRIES_BY_KIND[kind])
        if len(kind_valid) > cap:
            excess = len(kind_valid) - cap
            for _mtime, entry, _size, _kind in sorted(kind_valid)[:excess]:
                _remove_path(entry)
            kind_valid = sorted(kind_valid)[excess:]
        valid.extend(kind_valid)

    total = sum(size for _mtime, _entry, size, _kind in valid)
    if total > MAX_TOTAL_BYTES:
        kept = list(sorted(valid))
        for item in list(kept):
            if total <= MAX_TOTAL_BYTES:
                break
            _mtime, entry, size, _kind = item
            _remove_path(entry)
            total -= size
            kept.remove(item)
        valid = kept

    remaining = any(entry.exists() for _mtime, entry, _size, _kind in valid)
    print(f"Render durable cache sanitized: save_allowed={remaining}")
    return remaining
