from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import isco_video_agent.orchestrator as orchestrator
from isco_video_agent.ai_budget import Capability, Priority, TaskSpec


_original_tts_boundary: Callable[..., Any] | None = None


def _tts_spec(task_id: str) -> TaskSpec:
    """Mirror the pinned Engine's logical TTS task declaration exactly.

    The durable cache is installed only after Voice Mesh in production, so the local
    fallback hook is normally live and the Engine's per-task provider ceiling is 2.
    Keep the conditional nevertheless so isolated Engine-style tests preserve the same
    semantics if no Runner local fallback was installed.
    """
    runner_installed = (
        orchestrator.synthesize_local_wav is not orchestrator._default_synthesize_local_wav
    )
    return TaskSpec(
        task_id=task_id,
        kind="TTS_SECTION",
        priority=Priority.P0,
        capability=Capability.TTS,
        max_provider_attempts=2 if runner_installed else 1,
        schema_repair_allowed=False,
        local_fallback=True,
        semantic_block_is_final=False,
    )


def install_tts_cache_budget_accounting() -> None:
    """Keep cache hits visible as logical work without inventing provider attempts.

    Engine _ledger_authorize() normally performs register_task() immediately before a
    provider request. A durable cache hit deliberately never reaches that provider
    boundary, so this outer wrapper restores only the logical declaration. It never
    calls authorize() or record_attempt(); a hit is therefore one TTS logical task and
    zero provider attempts. On a miss the Engine later re-registers the same task_id
    (BudgetLedger explicitly allows replacement) and retains full retry ownership.
    """
    global _original_tts_boundary
    current = orchestrator._synthesize_tts_section
    if getattr(current, "_isco_tts_cache_budget_accounting", False) is True:
        return
    _original_tts_boundary = current

    def wrapped(
        ledger,
        circuit,
        budget,
        *,
        task_id: str,
        api_key: str,
        transcript: str,
        output: Path,
        model: str,
        voice: str,
        style: str,
    ) -> Path:
        if ledger is not None and str(task_id).startswith("TTS_SECTION_"):
            ledger.register_task(_tts_spec(str(task_id)))
        return _original_tts_boundary(
            ledger,
            circuit,
            budget,
            task_id=task_id,
            api_key=api_key,
            transcript=transcript,
            output=output,
            model=model,
            voice=voice,
            style=style,
        )

    wrapped._isco_tts_cache_budget_accounting = True
    wrapped._isco_tts_cache_budget_original = current
    orchestrator._synthesize_tts_section = wrapped
