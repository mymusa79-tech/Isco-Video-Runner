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

from dataclasses import dataclass
from enum import Enum

from scripts import cta_live_binding
from scripts import m7_live_binding
from scripts import m8_live_binding
from scripts import m9_live_binding
from scripts import m10_live_binding
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


def _required(flag: bool, label: str) -> None:
    if not flag:
        raise CinematicRuntimePortError(f"{label} did not install")


def install_cinematic_runtime_port(
    phase: CinematicInstallPhase | str,
) -> CinematicRuntimePortEvidence:
    """Install one certified Cinematic composition phase in historical order."""
    try:
        resolved = phase if isinstance(phase, CinematicInstallPhase) else CinematicInstallPhase(phase)
    except ValueError as exc:
        raise CinematicRuntimePortError(f"unknown Cinematic install phase: {phase!r}") from exc

    if resolved is CinematicInstallPhase.INNER:
        sfx_live_binding.install_sfx_live_binding()
        _required(bool(sfx_live_binding._INSTALLED), "SFX live binding")

        m8_live_binding.install_m8_live_binding()
        _required(bool(m8_live_binding._INSTALLED), "M8 live binding")

        m9_live_binding.install_m9_live_binding()
        _required(bool(m9_live_binding._INSTALLED), "M9 live binding")

        m10_live_binding.install_m10_live_binding()
        _required(bool(m10_live_binding._INSTALLED), "M10 live binding")

        cta_live_binding.install_cta_live_binding()
        _required(bool(cta_live_binding._INSTALLED), "CTA live binding")
    else:
        # M7 owns the certified M11 composition internally. Keeping that existing
        # owner intact avoids duplicating M11 installation or changing its nesting.
        m7_live_binding.install_m7_live_binding()
        _required(bool(m7_live_binding._INSTALLED), "M7/M11 live binding")

    return CinematicRuntimePortEvidence(
        port_id=PORT_ID,
        port_version=PORT_VERSION,
        stage_id=STAGE_ID,
        phase=resolved,
        provider_owner=PROVIDER_OWNER,
        retry_owner=RETRY_OWNER,
        sfx_installed=bool(sfx_live_binding._INSTALLED),
        m8_installed=bool(m8_live_binding._INSTALLED),
        m9_installed=bool(m9_live_binding._INSTALLED),
        m10_installed=bool(m10_live_binding._INSTALLED),
        cta_installed=bool(cta_live_binding._INSTALLED),
        m7_m11_installed=bool(m7_live_binding._INSTALLED),
    )
