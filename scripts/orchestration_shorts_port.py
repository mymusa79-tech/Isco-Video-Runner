from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import shorts_production_binding as core
from scripts.short_voice_v2 import apply_short_voice_v2


PORT_ID = "shorts-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "shorts"
PROVIDER_OWNER = "canonical-short-child-core"
RETRY_OWNER = "canonical-short-child-core"


def prepare_short_render(output_dir: Path, control_request: dict[str, Any]) -> dict[str, Any]:
    """Delegate Short pre-Gold preparation to the certified Shorts core exactly once."""
    return core.prepare_short_render(output_dir, control_request)


def prepare_authoritative_short_for_gold(
    output_dir: Path,
    control_request: dict[str, Any],
    *,
    ledger: Any,
    run_final_master_qc: Callable[[Path], dict[str, Any]],
) -> dict[str, Any]:
    """Own the one Short finishing seam that must complete before Gold.

    The core builds the progressive Short, Voice V2 owns voice/cinematic/SFX mutations,
    then the caller's already-composed Final Master QC surface is invoked again. Passing
    the composed surface is deliberate: Producer Handoff, Audio Semantic Integrity and
    durable Final QC remain in their existing owners and validate the exact final bytes.
    """
    pre_gold = core.prepare_short_render(output_dir, control_request)
    pre_gold = apply_short_voice_v2(
        output_dir,
        control_request,
        pre_gold,
        ledger=ledger,
    )
    master_qc = run_final_master_qc(output_dir)
    if master_qc.get("status") != "pass" or master_qc.get("final_media_mutated") is not False:
        raise RuntimeError("Short V2 authoritative Final Master QC did not pass")
    pre_gold["authoritative_final_master_qc_rerun"] = True
    return pre_gold


def finalize_short_quality(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
) -> dict[str, Any]:
    """Delegate post-Gold Short quality finalization to the certified core exactly once."""
    return core.finalize_short_quality(output_dir, control_request, pre_gold)
