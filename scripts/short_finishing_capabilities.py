from __future__ import annotations

"""Scoped in-process provider capabilities for post-core Short finishing.

Run #188 proved that the core correctly consumes production secrets exactly once, while
Short Voice V2 and the Short Cinematic Director later attempted to consume the same
source secrets again.  This adapter preserves the one-time secret owner and exposes only
request-scoped in-memory capabilities during the authoritative Short finishing seam.

No secret is reinserted into the environment, written to disk, logged, or retained after
the binding scope exits.  Legacy finishing modules keep their existing provider logic;
their imported ``secret`` readers are rebound once to ContextVar-backed resolvers.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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

        ``run_v3_voice`` is still the only source-secret reader.  This method accepts
        only the in-process values that it already passes to Gold and never reads env,
        files, or provider configuration itself.
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
    """Bind legacy secret call sites to scoped memory, never to source secrets.

    The assignment is process-stable and idempotent.  Request identity and secret values
    remain in the ContextVar, so concurrent contexts cannot see one another's values.
    """
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
    """Expose capabilities only while Short Voice/Cinematic finishing is executing."""
    if not isinstance(capabilities, ShortFinishingCapabilities):
        raise ShortFinishingCapabilityError("SHORT_FINISHING_CAPABILITY_TYPE_INVALID")
    _install_legacy_resolvers()
    token = _ACTIVE.set(capabilities)
    try:
        yield
    finally:
        _ACTIVE.reset(token)
