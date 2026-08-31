from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import attach_approval_binding


def materialize_approved_brief(request: dict[str, Any], output: Path) -> tuple[Path, str]:
    """Materialize an immutable Telegram-approved request as the Engine brief contract.

    This is an adapter only: it performs no dispatch, provider work, rendering, release,
    or state mutation. Production orchestration remains owned by the canonical V4 path.
    """
    fmt = "moment" if request.get("kind") == "short" else str(request.get("format") or "film")
    pack = request.get("approved_research_pack")
    if fmt in {"film", "story"}:
        if not isinstance(pack, list) or len(pack) < 2:
            raise RuntimeError("Long control production requires a completed approved research pack before dispatch")
    elif not isinstance(pack, list):
        pack = []
    brief = {
        "approved_by_user": True,
        "approved_topic": str(request.get("approved_topic") or "").strip(),
        "format": fmt,
        "approved_at": request.get("approved_at"),
        "weekly_option_id": request.get("weekly_option_id"),
        "research_pack": pack,
        "content_boundaries": request.get("content_boundaries") or [],
        "control_request_id": request.get("request_id"),
        "control_request_sha256": request.get("request_sha256"),
    }
    if not brief["approved_topic"]:
        raise RuntimeError("Control request has no approved topic")
    bound = attach_approval_binding(brief)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(bound, ensure_ascii=False, indent=2), encoding="utf-8")
    return output, str(bound["approved_hash"])
