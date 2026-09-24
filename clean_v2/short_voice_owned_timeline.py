from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .timeline_first import (
    CONTRACT_ID,
    TimelineFirstError,
    build_voice_owned_timeline,
    retime_events,
    section_duration_map,
)

ShortVoiceTimelineError = TimelineFirstError


def build_short_voice_owned_timeline(
    *,
    output_dir: Path,
    narration_path: Path,
) -> dict[str, Any]:
    """Compatibility seam over the shared Timeline First contract."""
    return build_voice_owned_timeline(
        output_dir=output_dir,
        narration_path=narration_path,
        fmt="short",
        require_identity=False,
    )
