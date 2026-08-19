from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
import re

import isco_video_agent.thumbnail as thumbnail
from isco_video_agent.ai_budget import BudgetLedger, Capability, Priority, TaskSpec
from isco_video_agent.orchestrator import _ledger_call, _ledger_call_status


_PREVIEW_RE = re.compile(r"(?P<concept>\d+)-preview-(?P<attempt>\d+)\.jpg$")


def _concept_spec() -> TaskSpec:
    return TaskSpec(
        task_id="GOLD_SHADOW_THUMBNAIL_CONCEPTS",
        kind="GOLD_SHADOW_THUMBNAIL_CONCEPTS",
        priority=Priority.P2,
        capability=Capability.TEXT,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=False,
    )


def _visual_spec(preview: Path) -> TaskSpec:
    match = _PREVIEW_RE.search(Path(preview).name)
    if match:
        suffix = f"C{int(match.group('concept')):02d}_A{int(match.group('attempt')):02d}"
    else:
        # Defensive deterministic fallback for future Packaging layouts. The preview
        # filename is never sent to a provider; it only names the logical ledger task.
        safe = re.sub(r"[^A-Za-z0-9]+", "_", Path(preview).stem).strip("_")[:48] or "UNKNOWN"
        suffix = safe.upper()
    return TaskSpec(
        task_id=f"GOLD_SHADOW_THUMBNAIL_VISUAL_{suffix}",
        kind="GOLD_SHADOW_THUMBNAIL_VISUAL",
        priority=Priority.P2,
        capability=Capability.VISION,
        max_provider_attempts=1,
        schema_repair_allowed=False,
        local_fallback=False,
        semantic_block_is_final=True,
    )


@contextmanager
def _budget_thumbnail_provider_calls(
    *,
    ledger: BudgetLedger,
    model: str,
) -> Iterator[None]:
    """Temporarily ledger the provider boundaries already owned by thumbnail.py.

    Packaging 360 remains the sole owner of thumbnail.py. This adapter does not copy
    its packaging logic and does not change its source file; it only wraps the two
    module-local Gemini callables during one synchronous Gold evaluation, then restores
    them in finally. Production is single-threaded at this boundary.
    """
    original_json_text = thumbnail.json_text
    original_audit_image_preview = thumbnail.audit_image_preview

    def budgeted_json_text(api_key: str, prompt: str, *, model: str):
        return _ledger_call(
            ledger,
            _concept_spec(),
            "gemini",
            model,
            original_json_text,
            api_key,
            prompt,
            model=model,
        )

    def budgeted_audit_image_preview(api_key: str, preview: Path, *args, **kwargs):
        resolved_model = str(kwargs.get("model") or model)
        return _ledger_call_status(
            ledger,
            _visual_spec(Path(preview)),
            "gemini",
            resolved_model,
            original_audit_image_preview,
            api_key,
            preview,
            *args,
            **kwargs,
        )

    thumbnail.json_text = budgeted_json_text
    thumbnail.audit_image_preview = budgeted_audit_image_preview
    try:
        yield
    finally:
        thumbnail.json_text = original_json_text
        thumbnail.audit_image_preview = original_audit_image_preview


def build_budgeted_thumbnail_package(
    *,
    gemini_key: str,
    pexels_key: str,
    plan,
    output_dir: Path,
    model: str,
    ledger: BudgetLedger,
    pixabay_key: str | None = None,
) -> dict:
    """Call the canonical thumbnail builder while accounting every Gemini attempt."""
    with _budget_thumbnail_provider_calls(ledger=ledger, model=model):
        return thumbnail.build_thumbnail_package(
            gemini_key=gemini_key,
            pexels_key=pexels_key,
            pixabay_key=pixabay_key,
            plan=plan,
            output_dir=output_dir,
            model=model,
        )
