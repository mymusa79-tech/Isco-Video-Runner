from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from isco_video_agent.brief_approval_binding import attach_approval_binding
from isco_video_agent.routing import choose_format_decision


LONG_FORMAT_POLICY_VERSION = "professional_long_format_router_v1"


def _format_context(request: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    candidate = request.get("candidate")
    if isinstance(candidate, dict):
        context["candidate"] = candidate
        pillar = str(candidate.get("pillar") or "").strip()
        if pillar:
            context["pillar"] = pillar
    pack = request.get("research_pack")
    if isinstance(pack, list):
        context["research_pack"] = pack
    return context


def resolve_control_format(request: dict[str, Any]) -> str:
    """Resolve the immutable outer format before the Approved Brief is signed.

    Legacy/explicit long requests keep their exact film/story choice. New Telegram
    long requests may carry ``auto`` only when they include the certified policy
    marker installed by the editorial control plane. The exact production Engine then
    makes the deterministic editorial-fit decision, and only the resolved film/story
    value is allowed into the cryptographically bound Approved Brief.
    """
    kind = str(request.get("kind") or "").strip()
    if kind == "short":
        return "moment"
    if kind != "long":
        raise RuntimeError("Control request kind is unsupported for Approved Brief materialization")

    requested = str(request.get("format") or "film").strip().lower()
    if requested in {"film", "story"}:
        return requested
    if requested != "auto":
        raise RuntimeError(f"Unsupported long control format: {requested or '<empty>'}")

    policy = request.get("format_policy")
    if not isinstance(policy, dict):
        raise RuntimeError("Long auto format request lacks certified Telegram format policy")
    if str(policy.get("version") or "") != LONG_FORMAT_POLICY_VERSION:
        raise RuntimeError("Long auto format request has unsupported format policy version")
    if str(policy.get("requested") or "") != "auto":
        raise RuntimeError("Long auto format policy requested value is invalid")
    if str(policy.get("resolution_stage") or "") != "v4_before_approved_brief_binding":
        raise RuntimeError("Long auto format policy resolution stage is invalid")

    topic = str(request.get("approved_topic") or "").strip()
    if not topic:
        raise RuntimeError("Control request has no approved topic")
    decision = choose_format_decision(topic, "auto", context=_format_context(request))
    if decision.format not in {"film", "story"}:
        raise RuntimeError(
            "Long format router produced a non-long format; kind/format contract is inconsistent"
        )
    return decision.format


def materialize_approved_brief(request: dict[str, Any], output: Path) -> tuple[Path, str]:
    """Materialize an immutable Telegram-approved request as the Engine brief contract.

    This is an adapter only: it performs no dispatch, provider work, rendering, release,
    or state mutation. Production orchestration remains owned by the canonical V4 path.
    """
    fmt = resolve_control_format(request)
    # The Telegram request schema owns this field as ``research_pack``. The previous
    # production adapter read a non-existent ``approved_research_pack`` field, causing
    # valid long-form requests to fail immediately before Planning. Keep one canonical
    # name at the boundary instead of supporting two drifting aliases.
    pack = request.get("research_pack")
    if fmt in {"film", "story"}:
        if not isinstance(pack, list) or len(pack) < 2:
            raise RuntimeError(
                "Long control production requires a completed approved research pack "
                "(research_pack) before dispatch"
            )
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
