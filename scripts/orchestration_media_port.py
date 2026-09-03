from __future__ import annotations

"""Stable orchestration port for the certified Media runtime topology.

This module owns composition only. It does not search providers, download bytes,
inspect media, retry provider calls, persist cache entries, or render derivatives.
Those behaviors remain owned by the already-certified Media implementations.
"""

from dataclasses import dataclass

from scripts import media_durable_cache
from scripts import media_prepared_live_cache
from scripts import media_search_durable_cache
from scripts import media_trust_boundary_v2
from scripts import provider_capacity_v2
from scripts.run184_qr_confirmation_closure import install_run184_qr_confirmation_closure

PORT_ID = "media-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "media"
PROVIDER_OWNER = "media-trust-security-core"
RETRY_OWNER = "media-trust-security-core"
CACHE_OWNER = "certified-media-cache-owners"


class MediaRuntimePortError(RuntimeError):
    """Fail-loud topology violation at the Media stable seam."""


@dataclass(frozen=True, slots=True)
class MediaRuntimePortEvidence:
    port_id: str
    port_version: int
    stage_id: str
    provider_owner: str
    retry_owner: str
    cache_owner: str
    provider_capacity_installed: bool
    trust_boundary_installed: bool
    durable_cache_configured: bool
    durable_asset_cache_installed: bool
    prepared_live_cache_installed: bool
    search_cache_installed: bool
    trust_revalidation_owner: str


def _durable_cache_configured() -> bool:
    return media_durable_cache._cache_root() is not None


def install_media_runtime_port() -> MediaRuntimePortEvidence:
    """Install the certified Media layers in their historical production order.

    Existing owners remain unchanged:
    - Provider Capacity V2 owns only the required Pixabay 24h metadata cache.
    - Media Trust Boundary V2 owns exact-byte provenance and Security V1 rechecks.
    - Run184 QR closure composes a mature local confirmation cascade into Security V1;
      it does not own stock selection, provider routing, or semantic Vision verdicts.
    - Media durable caches own their existing semantic namespaces and hit validation.
    - Provider/retry execution remains outside this orchestration port.
    """
    provider_capacity_v2.install_provider_capacity_v2()
    if not provider_capacity_v2._INSTALLED:
        raise MediaRuntimePortError("Provider Capacity V2 did not install")

    media_trust_boundary_v2.install_media_trust_boundary_v2()
    if not media_trust_boundary_v2._INSTALLED:
        raise MediaRuntimePortError("Media Trust Boundary V2 did not install")

    # Security V1 remains the authority. This composition step only replaces its QR
    # confirmation transport before any stock-media preflight can run.
    install_run184_qr_confirmation_closure()

    cache_configured = _durable_cache_configured()

    media_durable_cache.install_media_durable_cache()
    media_prepared_live_cache.install_media_prepared_live_cache()
    media_search_durable_cache.install_media_search_durable_cache()

    durable_installed = bool(media_durable_cache._INSTALLED)
    prepared_installed = bool(media_prepared_live_cache._INSTALLED)
    search_installed = bool(media_search_durable_cache._INSTALLED)

    if cache_configured and not durable_installed:
        raise MediaRuntimePortError("configured Media durable asset cache did not install")
    if cache_configured and not prepared_installed:
        raise MediaRuntimePortError("configured Media prepared-live cache did not install")
    if cache_configured and not search_installed:
        raise MediaRuntimePortError("configured Media search cache did not install")

    return MediaRuntimePortEvidence(
        port_id=PORT_ID,
        port_version=PORT_VERSION,
        stage_id=STAGE_ID,
        provider_owner=PROVIDER_OWNER,
        retry_owner=RETRY_OWNER,
        cache_owner=CACHE_OWNER,
        provider_capacity_installed=True,
        trust_boundary_installed=True,
        durable_cache_configured=cache_configured,
        durable_asset_cache_installed=durable_installed,
        prepared_live_cache_installed=prepared_installed,
        search_cache_installed=search_installed,
        trust_revalidation_owner="media-trust-boundary-v2+security-v1+run184-qr-confirmation-v1",
    )
