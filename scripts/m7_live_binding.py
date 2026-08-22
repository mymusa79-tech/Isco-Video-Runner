from __future__ import annotations

import os

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.cinematic_m7_live_binding import live_m7_binding_scope
from scripts.security_v1_live_binding import install_security_v1_live_binding


def install_m7_live_binding() -> None:
    """Install M7 final-render seams, then the outer Security V1 production boundary.

    The M7 wrapper captures provider keys from the in-process environment before the Engine
    consumes/removes them. Security V1 is installed after M7 so its preflight runs first at
    invocation time. Neither layer adds AI calls or changes provider routing.
    """
    current = orchestrator.produce
    if getattr(current, "_isco_m7_live_binding", False):
        install_security_v1_live_binding()
        return

    def wrapped(*args, **kwargs):
        pexels = (os.environ.get("PEXELS_API_KEY") or "").strip()
        pixabay = (os.environ.get("PIXABAY_API_KEY") or "").strip()
        if not pexels:
            # Preserve the core's own authoritative missing-secret failure.
            return current(*args, **kwargs)
        with live_m7_binding_scope(
            orchestrator,
            pexels_api_key=pexels,
            pixabay_api_key=pixabay,
        ):
            return current(*args, **kwargs)

    wrapped._isco_m7_live_binding = True
    wrapped._isco_m7_original = current
    orchestrator.produce = wrapped
    install_security_v1_live_binding()
