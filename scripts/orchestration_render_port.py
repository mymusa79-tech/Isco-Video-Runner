from __future__ import annotations

"""Stable orchestration port for the certified Render runtime topology.

This module owns composition only. Durable render cache semantics, fingerprints,
artifact validation, promotion/eviction, current Engine QC authority, and the
underlying render functions remain owned by the existing certified implementation.
"""

from dataclasses import dataclass

from scripts import render_durable_cache

PORT_ID = "render-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "render"
PROVIDER_OWNER = "render-durable-core"
RETRY_OWNER = "render-durable-core"
CACHE_OWNER = "render-durable-core"


class RenderRuntimePortError(RuntimeError):
    """Fail-loud topology violation at the Render stable seam."""


@dataclass(frozen=True, slots=True)
class RenderRuntimePortEvidence:
    port_id: str
    port_version: int
    stage_id: str
    provider_owner: str
    retry_owner: str
    cache_owner: str
    durable_cache_configured: bool
    durable_cache_installed: bool
    cache_namespace: str
    cache_schema_version: int
    current_qc_revalidation_required: bool


def install_render_runtime_port() -> RenderRuntimePortEvidence:
    """Install the certified durable Render layer without changing its semantics."""
    configured = render_durable_cache._shared_root() is not None
    render_durable_cache.install_render_durable_cache()
    installed = bool(render_durable_cache._INSTALLED)

    if configured and not installed:
        raise RenderRuntimePortError("configured Render durable cache did not install")

    return RenderRuntimePortEvidence(
        port_id=PORT_ID,
        port_version=PORT_VERSION,
        stage_id=STAGE_ID,
        provider_owner=PROVIDER_OWNER,
        retry_owner=RETRY_OWNER,
        cache_owner=CACHE_OWNER,
        durable_cache_configured=configured,
        durable_cache_installed=installed,
        cache_namespace=render_durable_cache.CACHE_NAMESPACE,
        cache_schema_version=render_durable_cache.CACHE_SCHEMA_VERSION,
        current_qc_revalidation_required=True,
    )
