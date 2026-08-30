from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import shorts_production_binding as core


PORT_ID = "shorts-runtime-port-v1"
PORT_VERSION = 1
STAGE_ID = "shorts"
PROVIDER_OWNER = "canonical-short-child-core"
RETRY_OWNER = "canonical-short-child-core"


def prepare_short_render(output_dir: Path, control_request: dict[str, Any]) -> dict[str, Any]:
    """Delegate Short pre-Gold preparation to the certified Shorts core exactly once."""
    return core.prepare_short_render(output_dir, control_request)


def finalize_short_quality(
    output_dir: Path,
    control_request: dict[str, Any],
    pre_gold: dict[str, Any],
) -> dict[str, Any]:
    """Delegate post-Gold Short quality finalization to the certified core exactly once."""
    return core.finalize_short_quality(output_dir, control_request, pre_gold)
