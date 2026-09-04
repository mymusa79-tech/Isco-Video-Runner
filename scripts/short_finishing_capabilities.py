from __future__ import annotations

"""Scoped provider capabilities for post-core Short finishing.

Run #188 proved that the core correctly consumes production secrets exactly once, while
Short Voice V2 and the Short Cinematic Director later attempted to consume the same
source secrets again.  This adapter preserves the one-time secret owner and exposes only
request-scoped in-memory capabilities during same-process Short finishing.

Sibling Shorts cross a real subprocess boundary after the long parent has already
consumed its source secrets.  For that boundary only, the parent materializes fresh
0600 one-time files inside RUNNER_TEMP and passes file *paths* to the child.  The Engine's
existing ``secret()`` contract consumes and deletes those files.  Secret values are never
placed back into environment variables, CLI arguments, logs, or persisted artifacts.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Iterator


class ShortFinishingCapabilityError(RuntimeError):
    """Fail-closed capability ownership violation without exposing secret material."""


@dataclass(frozen=True, slots=True)
class ShortFinishingCapabilities:
    gemini: str = field(repr=False)
    pexels: str = field(repr=False)
    pixabay: str | None = field(default=None, repr=False)

    @classmethod
    def from_gold_kwargs(cls, kwargs: dict[str, Any]) -> "ShortFinishingCapabilities":
        """Lease the already-owned production credentials passed to Gold.

        ``run_v3_voice`` remains the only source-secret reader.  This method accepts
        only the in-process values it already passes to Gold and never reads env/files.
        """
        gemini = str(kwargs.get("gemini") or "").strip()
        pexels = str(kwargs.get("pexels") or "").strip()
        raw_pixabay = kwargs.get("pixabay")
        pixabay = str(raw_pixabay).strip() if raw_pixabay else None
        if not gemini or not pexels:
            raise ShortFinishingCapabilityError(
                "SHORT_FINISHING_CAPABILITIES_MISSING required=gemini,pexels"
            )
        return cls(gemini=gemini, pexels=pexels, pixabay=pixabay)


_ACTIVE: ContextVar[ShortFinishingCapabilities | None] = ContextVar(
    "isco_short_finishing_capabilities", default=None
)
_CHILD_FILE_VARS = (
    "GEMINI_API_KEY_FILE",
    "PEXELS_API_KEY_FILE",
    "PIXABAY_API_KEY_FILE",
)


def _current() -> ShortFinishingCapabilities:
    capabilities = _ACTIVE.get()
    if capabilities is None:
        raise ShortFinishingCapabilityError(
            "SHORT_FINISHING_CAPABILITY_CONTEXT_MISSING"
        )
    return capabilities


def _voice_secret(name: str) -> str | None:
    if name != "GEMINI_API_KEY":
        raise ShortFinishingCapabilityError(
            f"SHORT_VOICE_CAPABILITY_NOT_ALLOWED name={name}"
        )
    return _current().gemini


def _cinematic_secret(name: str) -> str | None:
    capabilities = _current()
    if name == "GEMINI_API_KEY":
        return capabilities.gemini
    if name == "PEXELS_API_KEY":
        return capabilities.pexels
    if name == "PIXABAY_API_KEY":
        return capabilities.pixabay
    raise ShortFinishingCapabilityError(
        f"SHORT_CINEMATIC_CAPABILITY_NOT_ALLOWED name={name}"
    )


def _install_legacy_resolvers() -> None:
    """Bind legacy finishing readers to scoped memory, never source secrets."""
    from scripts import short_cinematic_director, short_voice_v2

    if getattr(short_voice_v2.secret, "_isco_short_capability_resolver", False) is not True:
        _voice_secret._isco_short_capability_resolver = True
        short_voice_v2.secret = _voice_secret
    if getattr(short_cinematic_director.secret, "_isco_short_capability_resolver", False) is not True:
        _cinematic_secret._isco_short_capability_resolver = True
        short_cinematic_director.secret = _cinematic_secret


@contextmanager
def bind_short_finishing_capabilities(
    capabilities: ShortFinishingCapabilities,
) -> Iterator[None]:
    """Expose capabilities only while Short Voice/Cinematic finishing executes."""
    if not isinstance(capabilities, ShortFinishingCapabilities):
        raise ShortFinishingCapabilityError("SHORT_FINISHING_CAPABILITY_TYPE_INVALID")
    _install_legacy_resolvers()
    token = _ACTIVE.set(capabilities)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def cleanup_child_capability_files(file_env: dict[str, str]) -> None:
    """Best-effort cleanup for files the child did not already consume/delete."""
    for name in _CHILD_FILE_VARS:
        raw = str(file_env.get(name) or "").strip()
        if not raw:
            continue
        try:
            Path(raw).unlink()
        except FileNotFoundError:
            pass


def materialize_child_capability_files(
    capabilities: ShortFinishingCapabilities,
    directory: Path,
) -> dict[str, str]:
    """Create fresh one-time files for an isolated sibling subprocess.

    The returned mapping contains file paths only.  Direct provider secret environment
    variables are deliberately not created here.
    """
    if not isinstance(capabilities, ShortFinishingCapabilities):
        raise ShortFinishingCapabilityError("SHORT_CHILD_CAPABILITY_TYPE_INVALID")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    values = (
        ("GEMINI_API_KEY_FILE", capabilities.gemini),
        ("PEXELS_API_KEY_FILE", capabilities.pexels),
        ("PIXABAY_API_KEY_FILE", capabilities.pixabay),
    )
    file_env: dict[str, str] = {}
    try:
        for file_var, value in values:
            if not value:
                continue
            path = root / f".{file_var.lower()}.one-time"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError as exc:
                raise ShortFinishingCapabilityError(
                    f"SHORT_CHILD_CAPABILITY_FILE_EXISTS name={file_var}"
                ) from exc
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
            os.chmod(path, 0o600)
            file_env[file_var] = str(path.resolve())
        if "GEMINI_API_KEY_FILE" not in file_env or "PEXELS_API_KEY_FILE" not in file_env:
            raise ShortFinishingCapabilityError(
                "SHORT_CHILD_CAPABILITIES_MISSING required=gemini,pexels"
            )
        return file_env
    except Exception:
        cleanup_child_capability_files(file_env)
        raise
