from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import isco_video_agent.orchestrator as orchestrator

from scripts import cta_live_binding
from scripts import m10_live_binding
from scripts import m9_live_binding
from scripts import narrative_music_dynamics
from scripts import sfx_live_binding

CACHE_SCHEMA_VERSION = 1
CACHE_NAMESPACE = "render-durable-v1"
MAX_FILE_BYTES = 1536 * 1024 * 1024
MAX_SIDECAR_BYTES = 2 * 1024 * 1024
MAX_ENTRIES_PER_KIND = 4
MAX_TOTAL_BYTES = 3 * 1024 * 1024 * 1024

_KIND_PICTURE = "picture"
_KIND_BURN = "burn"
_KIND_FINAL = "final"
_KINDS = (_KIND_PICTURE, _KIND_BURN, _KIND_FINAL)

_FINAL_SIDECARS = (
    "sfx-plan.json",
    "m10-cards.json",
    "cta-plan.json",
    "narrative-music-dynamics.json",
)
_PICTURE_SIDECARS = ("m9-transitions.json",)

_INSTALLED = False
_ORIGINAL_PRODUCE: Callable[..., Any] | None = None


def _shared_root() -> Path | None:
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


def _artifact_sha(root: Path, name: str) -> str | None:
    path = Path(root) / name
    if path.is_symlink() or not path.is_file():
        return None
    return _sha256_file(path)


def _optional_file_binding(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        return None
    return {
        "name": path.name,
        "sha256": _sha256_file(path),
        "byte_length": path.stat().st_size,
        "suffix": path.suffix.lower(),
    }


def _base_binding(kind: str) -> dict[str, Any]:
    return {
        "namespace": CACHE_NAMESPACE,
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "approved_brief_sha256": _required_env("ISCO_APPROVED_BRIEF_SHA256"),
        "engine_sha": _required_env("ISCO_ENGINE_SHA"),
        "render_contract_sha256": _module_sha(sys.modules[__name__]),
    }


def _picture_binding(inputs: list[Path], renderer: Callable[..., Any], output: Path) -> dict[str, Any]:
    root = Path(output).parent
    return {
        **_base_binding(_KIND_PICTURE),
        "renderer": _callable_identity(renderer),
        "m9_live_sha256": _module_sha(m9_live_binding),
        "ordered_inputs": [
            {
                "name": Path(path).name,
                "sha256": _sha256_file(path),
                "byte_length": Path(path).stat().st_size,
            }
            for path in inputs
        ],
        "visual_timeline_sha256": _artifact_sha(root, "visual-timeline.json"),
    }


def _burn_binding(video: Path, srt: Path, renderer: Callable[..., Any], *, portrait: bool) -> dict[str, Any]:
    return {
        **_base_binding(_KIND_BURN),
        "renderer": _callable_identity(renderer),
        "video": _optional_file_binding(Path(video)),
        "srt": _optional_file_binding(Path(srt)),
        "portrait": bool(portrait),
    }


def _final_binding(
    video: Path,
    narration: Path | None,
    renderer: Callable[..., Any],
    output: Path,
    music: Path | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    root = Path(output).parent
    semantic_kwargs = {
        key: kwargs[key]
        for key in sorted(kwargs)
        if key in {"target_lufs", "music_gain", "outro_seconds", "opening_director"}
    }
    return {
        **_base_binding(_KIND_FINAL),
        "renderer": _callable_identity(renderer),
        "video": _optional_file_binding(Path(video)),
        "narration": _optional_file_binding(Path(narration)) if narration is not None else None,
        "music": _optional_file_binding(Path(music)) if music is not None else None,
        "plan_sha256": _artifact_sha(root, "plan.json"),
        "visual_timeline_sha256": _artifact_sha(root, "visual-timeline.json"),
        "runner_mux_chain": {
            "sfx": _module_sha(sfx_live_binding),
            "m10": _module_sha(m10_live_binding),
            "cta": _module_sha(cta_live_binding),
            "narrative_music": _module_sha(narrative_music_dynamics),
        },
        "kwargs": semantic_kwargs,
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


def _capture_sidecars(output_root: Path, names: tuple[str, ...], entry: Path) -> dict[str, dict[str, Any]]:
    captured: dict[str, dict[str, Any]] = {}
    sidecar_root = entry / "sidecars"
    for name in names:
        source = Path(output_root) / name
        if source.is_symlink() or not source.is_file() or _load_json(source) is None:
            continue
        size = source.stat().st_size
        if size <= 0 or size > MAX_SIDECAR_BYTES:
            continue
        target = sidecar_root / name
        _atomic_copy(source, target)
        captured[name] = {
            "sha256": _sha256_file(target),
            "byte_length": target.stat().st_size,
        }
    return captured


def _validate_sidecars(entry: Path, manifest: dict[str, Any]) -> dict[str, Path] | None:
    raw = manifest.get("sidecars", {})
    if not isinstance(raw, dict):
        return None
    result: dict[str, Path] = {}
    for name, meta in raw.items():
        if name != Path(name).name or not isinstance(meta, dict):
            return None
        path = entry / "sidecars" / name
        if path.is_symlink() or not path.is_file() or _load_json(path) is None:
            return None
        size = path.stat().st_size
        if size <= 0 or size > MAX_SIDECAR_BYTES or meta.get("byte_length") != size:
            return None
        sha = str(meta.get("sha256") or "")
        if not sha or _sha256_file(path) != sha:
            return None
        result[name] = path
    return result


def _picture_is_cacheable(output_root: Path) -> bool:
    report = _load_json(Path(output_root) / "m9-transitions.json")
    if report is None or str(report.get("status") or "") != "applied":
        return False
    try:
        return int(report.get("dissolve_count") or 0) > 0
    except (TypeError, ValueError):
        return False


def _final_reports_cacheable(output_root: Path) -> bool:
    """Never memorialize transient optional-polish fallbacks as the durable final."""
    root = Path(output_root)
    for name in _FINAL_SIDECARS:
        path = root / name
        if not path.exists():
            continue
        report = _load_json(path)
        if report is None:
            return False
        status = str(report.get("status") or "").strip().casefold()
        if "error" in status or "fallback" in status:
            return False
    return True


def _validate_entry(root: Path, kind: str, fingerprint: str, binding: dict[str, Any] | None = None):
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
        or manifest.get("fingerprint") != _binding_hash(stored_binding)
        or manifest.get("fingerprint") != fingerprint
    ):
        return None
    if binding is not None and stored_binding != binding:
        return None
    if stored_binding.get("render_contract_sha256") != _module_sha(sys.modules[__name__]):
        return None
    size = artifact.stat().st_size
    if size <= 0 or size > MAX_FILE_BYTES or manifest.get("byte_length") != size:
        return None
    sha = str(manifest.get("sha256") or "")
    if not sha or _sha256_file(artifact) != sha:
        return None
    sidecars = _validate_sidecars(entry, manifest)
    if sidecars is None:
        return None
    return artifact, sidecars, manifest


def _persist_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any],
    source: Path,
    *,
    sidecar_names: tuple[str, ...] = (),
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
    sidecars = _capture_sidecars(source.parent, sidecar_names, entry)
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "kind": kind,
        "fingerprint": fingerprint,
        "binding": binding,
        "sha256": _sha256_file(artifact),
        "byte_length": artifact.stat().st_size,
        "sidecars": sidecars,
    }
    _atomic_json(entry / "manifest.json", manifest)


def _restore_entry(
    root: Path,
    kind: str,
    fingerprint: str,
    binding: dict[str, Any],
    dest: Path,
    *,
    known_sidecars: tuple[str, ...] = (),
) -> bool:
    valid = _validate_entry(root, kind, fingerprint, binding)
    if valid is None:
        return False
    artifact, sidecars, _manifest = valid
    dest = Path(dest)
    try:
        _atomic_copy(artifact, dest)
        for name in known_sidecars:
            (dest.parent / name).unlink(missing_ok=True)
        for name, cached in sidecars.items():
            _atomic_copy(cached, dest.parent / name)
        print(f"Render durable {kind} HIT fingerprint={fingerprint[:12]}")
        return True
    except Exception as exc:
        dest.unlink(missing_ok=True)
        print(f"Render durable {kind} restore rejected ({type(exc).__name__})")
        return False


def _wrap_concat(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(inputs, output):
        output = Path(output)
        paths = [Path(item) for item in inputs]
        if output.name != "picture.mp4" or not paths or any(not p.is_file() or p.is_symlink() for p in paths):
            return original(paths, output)
        binding = _picture_binding(paths, original, output)
        fingerprint = _binding_hash(binding)
        if _restore_entry(
            root,
            _KIND_PICTURE,
            fingerprint,
            binding,
            output,
            known_sidecars=_PICTURE_SIDECARS,
        ):
            return output
        result = Path(original(paths, output))
        if not _picture_is_cacheable(result.parent):
            return result
        try:
            _persist_entry(
                root,
                _KIND_PICTURE,
                fingerprint,
                binding,
                result,
                sidecar_names=_PICTURE_SIDECARS,
            )
        except Exception as exc:
            print(f"Render durable picture persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_render_durable_concat = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _wrap_burn(root: Path, original: Callable[..., Path]) -> Callable[..., Path]:
    def wrapped(video: Path, srt: Path, output: Path, *, portrait: bool = False) -> Path:
        video = Path(video)
        srt = Path(srt)
        output = Path(output)
        if output.name != "picture-text.mp4" or not video.is_file() or not srt.is_file():
            return original(video, srt, output, portrait=portrait)
        binding = _burn_binding(video, srt, original, portrait=portrait)
        fingerprint = _binding_hash(binding)
        if _restore_entry(root, _KIND_BURN, fingerprint, binding, output):
            return output
        result = Path(original(video, srt, output, portrait=portrait))
        try:
            _persist_entry(root, _KIND_BURN, fingerprint, binding, result)
        except Exception as exc:
            print(f"Render durable burn persistence skipped ({type(exc).__name__})")
        return result

    wrapped._isco_render_durable_burn = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _quality_passed(output_root: Path) -> bool:
    quality = _load_json(Path(output_root) / "quality-final.json")
    if quality is None:
        return False
    return bool(
        quality.get("duration_ok") is True
        and quality.get("audio_ok") is True
        and quality.get("av_sync_ok") is True
        and int(quality.get("video_streams") or 0) >= 1
    )


def _wrap_mux(root: Path, original: Callable[..., Path], pending: list[dict[str, Any]]) -> Callable[..., Path]:
    def wrapped(video, narration, output, music=None, **kwargs):
        output = Path(output)
        video = Path(video)
        narration_path = Path(narration) if narration is not None else None
        music_path = Path(music) if music is not None else None
        if output.name != "final.mp4" or not video.is_file():
            return original(video, narration, output, music=music, **kwargs)
        if narration_path is None and music_path is None:
            return original(video, narration, output, music=music, **kwargs)
        binding = _final_binding(video, narration_path, original, output, music_path, kwargs)
        fingerprint = _binding_hash(binding)
        hit = _restore_entry(
            root,
            _KIND_FINAL,
            fingerprint,
            binding,
            output,
            known_sidecars=_FINAL_SIDECARS,
        )
        pending.append(
            {
                "root": output.parent,
                "dest": output,
                "fingerprint": fingerprint,
                "binding": binding,
                "hit": hit,
            }
        )
        if hit:
            return output
        return Path(original(video, narration, output, music=music, **kwargs))

    wrapped._isco_render_durable_mux = True
    wrapped._isco_render_durable_original = original
    return wrapped


def _reconcile_final_candidates(cache_root: Path, pending: list[dict[str, Any]]) -> None:
    for item in pending:
        output_root = Path(item["root"])
        dest = Path(item["dest"])
        fingerprint = str(item["fingerprint"])
        binding = item["binding"]
        hit = bool(item["hit"])
        passed = _quality_passed(output_root) and _final_reports_cacheable(output_root)
        if passed:
            if not hit:
                try:
                    _persist_entry(
                        cache_root,
                        _KIND_FINAL,
                        fingerprint,
                        binding,
                        dest,
                        sidecar_names=_FINAL_SIDECARS,
                    )
                    print(f"Render durable final PROMOTED after QC fingerprint={fingerprint[:12]}")
                except Exception as exc:
                    print(f"Render durable final promotion skipped ({type(exc).__name__})")
            continue
        if hit:
            shutil.rmtree(_entry(cache_root, _KIND_FINAL, fingerprint), ignore_errors=True)
            print(f"Render durable final EVICTED after current QC/polish did not certify fingerprint={fingerprint[:12]}")


@contextmanager
def render_durable_scope() -> Iterator[None]:
    """Wrap the already-active cinematic/render seams while leaving Audio Integrity outside."""
    root = _shared_root()
    if root is None:
        yield
        return
    root.mkdir(parents=True, exist_ok=True)

    original_concat = orchestrator.concat_video
    original_burn = orchestrator.burn_srt
    original_mux = orchestrator.mux
    pending: list[dict[str, Any]] = []

    orchestrator.concat_video = _wrap_concat(root, original_concat)
    orchestrator.burn_srt = _wrap_burn(root, original_burn)
    orchestrator.mux = _wrap_mux(root, original_mux, pending)
    try:
        yield
    finally:
        try:
            _reconcile_final_candidates(root, pending)
        finally:
            if getattr(orchestrator.concat_video, "_isco_render_durable_concat", False):
                orchestrator.concat_video = original_concat
            if getattr(orchestrator.burn_srt, "_isco_render_durable_burn", False):
                orchestrator.burn_srt = original_burn
            if getattr(orchestrator.mux, "_isco_render_durable_mux", False):
                orchestrator.mux = original_mux


def install_render_durable_cache() -> None:
    """Install after Audio Semantic Integrity and before later cinematic produce wrappers."""
    global _INSTALLED, _ORIGINAL_PRODUCE
    if _INSTALLED:
        return
    if _shared_root() is None:
        print("Render durable cache disabled: durable stage cache not configured")
        return
    current = orchestrator.produce
    if getattr(current, "_isco_render_durable_produce", False):
        _INSTALLED = True
        return
    _ORIGINAL_PRODUCE = current

    def wrapped(*args, **kwargs):
        with render_durable_scope():
            return current(*args, **kwargs)

    wrapped._isco_render_durable_produce = True
    wrapped._isco_render_durable_original = current
    orchestrator.produce = wrapped
    _INSTALLED = True
    print("Render durable cache installed: M9 assembly/burn/final render resume enabled with QC promotion")


def reset_render_durable_cache_for_tests() -> None:
    global _INSTALLED, _ORIGINAL_PRODUCE
    if _ORIGINAL_PRODUCE is not None and getattr(orchestrator.produce, "_isco_render_durable_produce", False):
        orchestrator.produce = _ORIGINAL_PRODUCE
    _ORIGINAL_PRODUCE = None
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
    """Validate and cap Render only; older TTS/Media semantic namespaces stay independent."""
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
            artifact, sidecars, _manifest = checked
            size = artifact.stat().st_size + sum(path.stat().st_size for path in sidecars.values())
            item = (entry.stat().st_mtime, entry, size, kind)
            kind_valid.append(item)
        if len(kind_valid) > MAX_ENTRIES_PER_KIND:
            excess = len(kind_valid) - MAX_ENTRIES_PER_KIND
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
