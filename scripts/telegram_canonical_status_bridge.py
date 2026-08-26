from __future__ import annotations

from typing import Any

from scripts import telegram_production_rich_ui as production_rich
from scripts import telegram_rich_integration as rich_integration
from scripts.telegram_status_model import stage_for_step, status_label

_INSTALLED = False


def _canonical_step_stage(step_name: str) -> str:
    return stage_for_step(step_name)["label"]


def _canonical_stage_label(value: Any) -> str:
    return status_label(value)


def install() -> None:
    """Route legacy Telegram status presentation through the canonical V1 contract.

    Kept as a small compatibility bridge so we can retire duplicate interpretation
    incrementally without destabilizing the already-tested editorial control surface.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    rich_integration._step_stage = _canonical_step_stage
    production_rich._stage_label = _canonical_stage_label
    _INSTALLED = True
