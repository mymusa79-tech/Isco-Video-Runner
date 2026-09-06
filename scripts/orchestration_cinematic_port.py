from __future__ import annotations

"""Stable orchestration port for the certified Cinematic runtime topology.

Cinematic composition is historically split across two installation moments. The
INNER phase installs the produce wrappers that must exist before Render durability;
the OUTER phase installs the M7/M11 produce authority after TTS and before the
opening-feasibility guard. Keeping those phases explicit preserves wrapper nesting
without making stage identity depend on wrapper position.

This module owns composition only. It does not select providers, retry provider
calls, render frames, mutate quality thresholds, or change any cinematic policy.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum

import isco_video_agent.orchestrator as orchestrator
from scripts import cta_live_binding
from scripts import m7_live_binding
from scripts import m8_live_binding
from scripts import m9_live_binding
from scripts import m10_live_binding
from scripts import run214_canonical_visual_intent
from scripts import sfx_live_binding

PORT_ID = "cinematic-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "cinematic"
PROVIDER_OWNER = "certified-cinematic-core"
RETRY_OWNER = "certified-cinematic-core"


class CinematicInstallPhase(str, Enum):
    INNER = "inner"
    OUTER = "outer"


class CinematicRuntimePortError(RuntimeError):
    """Fail-loud topology violation at the Cinematic stable seam."""


@dataclass(frozen=True, slots=True)
class CinematicRuntimePortEvidence:
    port_id: str
    port_version: int
    stage_id: str
    phase: CinematicInstallPhase
    provider_owner: str
    retry_owner: str
    sfx_installed: bool
    m8_installed: bool
    m9_installed: bool
    m10_installed: bool
    cta_installed: bool
    m7_m11_installed: bool


def _wrapper_chain() -> Iterator[Callable[..., object]]:
    """Walk the installed Runner wrapper topology without depending on module flags."""
    pending = [orchestrator.produce]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        identity = id(current)
        if identity in seen:
            continue
        seen.add(identity)
        yield current
        namespace = getattr(current, "__dict__", {})
        for name, value in namespace.items():
            if name.startswith("_isco_") and name.endswith("_original") and callable(value):
                pending.append(value)


def _binding_installed(marker: str) -> bool:
    return any(bool(getattr(layer, marker, False)) for layer in _wrapper_chain())


def _required(flag: bool, label: str) -> None:
    if not flag:
        raise CinematicRuntimePortError(f"{label} did not install")


def _install_required(installer: Callable[[], None], marker: str, label: str) -> None:
    """Install once, then verify the binding from the real wrapper topology."""
    if _binding_installed(marker):
        return
    installer()
    _required(_binding_installed(marker), label)


def install_cinematic_runtime_port(
    phase: CinematicInstallPhase | str,
) -> CinematicRuntimePortEvidence:
    """Install one certified Cinematic composition phase in historical order."""
    try:
        resolved = phase if isinstance(phase, CinematicInstallPhase) else CinematicInstallPhase(phase)
    except ValueError as exc:
        raise CinematicRuntimePortError(f"unknown Cinematic install phase: {phase!r}") from exc

    if resolved is CinematicInstallPhase.INNER:
        _install_required(
            sfx_live_binding.install_sfx_live_binding,
            "_isco_sfx_live_binding",
            "SFX live binding",
        )
        _install_required(
            m8_live_binding.install_m8_live_binding,
            "_isco_m8_live_binding",
            "M8 live binding",
        )
        _install_required(
            m9_live_binding.install_m9_live_binding,
            "_isco_m9_live_binding",
            "M9 live binding",
        )
        _install_required(
            m10_live_binding.install_m10_live_binding,
            "_isco_m10_live_binding",
            "M10 live binding",
        )
        _install_required(
            cta_live_binding.install_cta_live_binding,
            "_isco_cta_live_binding",
            "CTA live binding",
        )
    else:
        # M7 owns the certified M11 composition internally. Keeping that existing
        # owner intact avoids duplicating M11 installation or changing its nesting.
        _install_required(
            m7_live_binding.install_m7_live_binding,
            "_isco_m7_live_binding",
            "M7/M11 live binding",
        )
        # Run214 must bind only after M7 has installed Visual V1 + Run183/185 + the
        # runtime scope, but before Opening Feasibility captures the final selector
        # surfaces. That ordering makes CanonicalVisualIntent the last visual-truth
        # owner without changing M7/M11's own responsibility or nesting.
        run214_canonical_visual_intent.install_run214_canonical_visual_intent()

    return CinematicRuntimePortEvidence(
        port_id=PORT_ID,
        port_version=PORT_VERSION,
        stage_id=STAGE_ID,
        phase=resolved,
        provider_owner=PROVIDER_OWNER,
        retry_owner=RETRY_OWNER,
        sfx_installed=_binding_installed("_isco_sfx_live_binding"),
        m8_installed=_binding_installed("_isco_m8_live_binding"),
        m9_installed=_binding_installed("_isco_m9_live_binding"),
        m10_installed=_binding_installed("_isco_m10_live_binding"),
        cta_installed=_binding_installed("_isco_cta_live_binding"),
        m7_m11_installed=_binding_installed("_isco_m7_live_binding"),
    )