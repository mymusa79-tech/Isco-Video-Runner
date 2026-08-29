from __future__ import annotations

"""Stable orchestration port for the certified TTS runtime topology.

This module does not synthesize audio, call providers, retry, cache bytes, or own
quality policy. It owns only the installation seam that composes the already-certified
Voice Mesh -> durable TTS cache -> Voice Identity Observer layers in their historical
production order. Audio Semantic Integrity remains a produce-scope gate and is not
moved into this port.
"""

from dataclasses import dataclass
import os

import isco_video_agent.orchestrator as orchestrator

from scripts import tts_durable_cache, voice_identity_observer, voice_mesh

PORT_ID = "tts-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "tts"
PROVIDER_OWNER = "legacy-voice-mesh-core"
RETRY_OWNER = "legacy-voice-mesh-core"
CACHE_OWNER = "tts-durable-cache-semantics"


class TTSRuntimePortError(RuntimeError):
    """Fail-loud topology violation at the TTS stable seam."""


@dataclass(frozen=True, slots=True)
class TTSRuntimePortEvidence:
    port_id: str
    port_version: int
    stage_id: str
    provider_owner: str
    retry_owner: str
    cache_owner: str
    voice_mesh_installed: bool
    durable_cache_configured: bool
    durable_cache_installed: bool
    voice_identity_observer_installed: bool
    audio_semantic_integrity_owner: str


def _cache_configured() -> bool:
    return bool(str(os.environ.get("ISCO_TTS_CACHE_PATH") or "").strip())


def _current_evidence() -> TTSRuntimePortEvidence:
    current = orchestrator._synthesize_tts_section
    cached = getattr(current, "_isco_tts_runtime_port_evidence", None)
    if not isinstance(cached, TTSRuntimePortEvidence):
        raise TTSRuntimePortError("TTS runtime port marker exists without typed evidence")
    return cached


def install_tts_runtime_port() -> TTSRuntimePortEvidence:
    """Install the certified TTS layers exactly once behind one stable entrypoint.

    Existing implementation owners remain unchanged:
    - Voice Mesh owns Gemini/Piper provider selection and provider retry certification.
    - TTS durable cache owns semantic reuse and current-hit revalidation.
    - Voice Identity Observer remains observe-only and wraps the final section boundary.
    - Audio Semantic Integrity remains outside this seam at the produce() scope.
    """
    current = orchestrator._synthesize_tts_section
    if getattr(current, "_isco_tts_runtime_port", False) is True:
        return _current_evidence()

    voice_mesh.install_voice_mesh()
    if orchestrator.synthesize_wav is not voice_mesh.synthesize:
        raise TTSRuntimePortError("Voice Mesh cloud boundary was not installed")
    if orchestrator.synthesize_local_wav is not voice_mesh.synthesize_local_wav:
        raise TTSRuntimePortError("Voice Mesh local fallback boundary was not installed")

    tts_durable_cache.install_tts_durable_cache()
    cache_boundary = orchestrator._synthesize_tts_section
    cache_configured = _cache_configured()
    cache_installed = getattr(cache_boundary, "_is_tts_durable_cache", False) is True
    if cache_configured and not cache_installed:
        raise TTSRuntimePortError("configured TTS durable cache did not own the section boundary")

    voice_identity_observer.install_voice_identity_observer()
    final_boundary = orchestrator._synthesize_tts_section
    observer_installed = getattr(final_boundary, "_is_voice_identity_observer", False) is True
    if not observer_installed:
        raise TTSRuntimePortError("Voice Identity Observer did not wrap the final TTS boundary")

    observer_inner = voice_identity_observer._original_synthesize_tts_section
    if cache_configured:
        if getattr(observer_inner, "_is_tts_durable_cache", False) is not True:
            raise TTSRuntimePortError("TTS durable cache is not inside Voice Identity Observer")
        if observer_inner is not cache_boundary:
            raise TTSRuntimePortError("TTS boundary changed between cache and observer installation")

    evidence = TTSRuntimePortEvidence(
        port_id=PORT_ID,
        port_version=PORT_VERSION,
        stage_id=STAGE_ID,
        provider_owner=PROVIDER_OWNER,
        retry_owner=RETRY_OWNER,
        cache_owner=CACHE_OWNER,
        voice_mesh_installed=True,
        durable_cache_configured=cache_configured,
        durable_cache_installed=cache_installed,
        voice_identity_observer_installed=True,
        audio_semantic_integrity_owner="produce-scope-existing-owner",
    )
    final_boundary._isco_tts_runtime_port = True
    final_boundary._isco_tts_runtime_port_evidence = evidence
    return evidence
