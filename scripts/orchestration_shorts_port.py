from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import short_voice_v2
from scripts import shorts_production_binding as core
from scripts.run212_visual_candidate_utilization import short_candidate_utilization_scope
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
    existing owners; no quality gate is weakened here.
    """
    pre_gold = core.prepare_short_render(output_dir, control_request)
    # Short Cinematic executes after orchestrator.produce(), so re-enter the canonical
    # Visual Retrieval/Vision policy only for this finishing request. Run212 composes the
    # existing Run200 availability recovery with bounded candidate-utilization headroom,
    # then restores every imported-by-value Short surface in finally.
    with short_candidate_utilization_scope(output_dir):
        pre_gold = apply_voice_owned_short(
            output_dir,
            control_request,
            pre_gold,
            ledger=ledger,
        )

    # Run219 closure: Voice-Owned Timeline is imported before production.main() installs
    # runtime wrappers, so an imported-by-value quality refresh can retain the historical
    # callable and bypass the live Audio Producer short_finished wrapper. Re-enter the
    # module-owned surface *after* all Voice/Cinematic/SFX mutations. This makes the
    # already-installed Audio Producer lifecycle measure/repair (at most once) and bind
    # its receipt to the exact finished final.mp4 bytes before independent Final Master
    # QC. No provider/AI call or retry is introduced here.
    short_voice_v2._refresh_quality_final(
        Path(output_dir),
        Path(output_dir) / "final.mp4",
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
    """Delegate post-Gold Short quality finalization to the certified core exactly once."""
    return core.finalize_short_quality(output_dir, control_request, pre_gold)
