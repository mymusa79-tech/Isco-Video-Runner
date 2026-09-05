from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import shorts_production_binding as core
from scripts.short_voice_owned_timeline import apply_voice_owned_short


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

    The core builds the progressive visual candidate, Voice-Owned Timeline V1 synthesizes
    the natural performance and makes the visual timeline follow measured narration,
    then the caller's already-composed Final Master QC surface validates the exact bytes.
    Producer Handoff, Audio Semantic Integrity and durable Final QC remain in their
    existing owners; no quality gate or retry budget is weakened here.
    """
    pre_gold = core.prepare_short_render(output_dir, control_request)
    pre_gold = apply_voice_owned_short(
        output_dir,
        control_request,
        pre_gold,
        ledger=ledger,
    )
    master_qc = run_final_master_qc(output_dir)
    if master_qc.get("status") != "pass" or master_qc.get("final_media_mutated") is not False:
        raise RuntimeError("Voice-Owned Short authoritative Final Master QC did not pass")
    pre_gold["authoritative_final_master_qc_rerun"] = True
    return pre_gold


def finalize_short_quality(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
) -> dict[str, Any]:
    """Finalize core once, then certify Engine-owned promise continuity explicitly.

    The core remains the sole Short quality finalizer. Promise continuity is a post-core
    certification owned by this orchestration port, so import order cannot mutate the
    core function or create a second finalization path.
    """
    report = core.finalize_short_quality(output_dir, control_request, pre_gold)
    from scripts.editorial_promise_continuity import certify_short_delivery

    return certify_short_delivery(output_dir, control_request, report)
