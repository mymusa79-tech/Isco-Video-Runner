from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from scripts.channel_os_memory import ChannelOSMemory, LiveStateProvider
from scripts.channel_os_mission_control import (
    MISSION_STATES,
    GitHubActionsLiveStateProvider,
    MissionControl,
    MissionSnapshot,
    VideoEntity,
    render_telegram,
)

CHANNEL_OS_CALLBACKS = {
    "cmd:channelos-refresh": "all",
    "cmd:channelos-needs": "needs",
    "cmd:channelos-problems": "problems",
}
CHANNEL_OS_TEXT_COMMANDS = {
    "channel os",
    "channelos",
    "لوحة القناة",
    "نظام القناة",
    "حالة القناة",
}


def callback_view(data: object) -> str | None:
    return CHANNEL_OS_CALLBACKS.get(str(data or "").strip())


def text_view(text: object) -> str | None:
    normalized = " ".join(str(text or "").strip().casefold().split())
    return "all" if normalized in CHANNEL_OS_TEXT_COMMANDS else None


def _latest_runs(state: Mapping[str, Any]) -> dict[str, str]:
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        return {}
    latest: dict[str, tuple[tuple[str, int], str]] = {}
    for item in queue:
        if not isinstance(item, Mapping):
            continue
        request_id = str(item.get("request_id") or "").strip()
        run_id = str(item.get("workflow_run_id") or "").strip()
        if not request_id or not run_id.isdigit():
            continue
        try:
            attempt = int(item.get("attempt") or 0)
        except (TypeError, ValueError):
            continue
        stamp = str(item.get("consumed_at") or item.get("reserved_at") or item.get("requested_at") or "")
        rank = (stamp, attempt)
        previous = latest.get(request_id)
        if previous is None or rank > previous[0]:
            latest[request_id] = (rank, run_id)
    return {request_id: value[1] for request_id, value in latest.items()}


def video_entities_from_control_state(state: Mapping[str, Any]) -> tuple[VideoEntity, ...]:
    """Project existing Telegram control-plane truth into Channel OS entities.

    No YouTube publication state is inferred. Approved requests are durable `Ready`;
    live GitHub Actions can transiently project them to Producing / Needs Me / Problems.
    Saved, unselected suggestions are Ideas. A successful production run therefore
    returns to Ready until a separate durable publication state exists.
    """
    entities: list[VideoEntity] = []
    seen_ids: set[str] = set()

    saved = state.get("saved_suggestions")
    if isinstance(saved, list):
        for item in saved:
            if not isinstance(item, Mapping) or item.get("status") not in {None, "available", "saved"}:
                continue
            archive_id = str(item.get("archive_id") or "").strip()
            candidate = item.get("candidate")
            title = str(candidate.get("title") or "").strip() if isinstance(candidate, Mapping) else ""
            if not archive_id or not title:
                continue
            video_id = f"saved:{archive_id}"
            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            entities.append(VideoEntity(video_id=video_id, title=title, durable_state="Ideas"))

    latest_runs = _latest_runs(state)
    requests = state.get("requests")
    if isinstance(requests, Mapping):
        ordered = sorted(
            (item for item in requests.values() if isinstance(item, Mapping)),
            key=lambda item: (str(item.get("approved_at") or ""), str(item.get("request_id") or "")),
        )
        for request in ordered:
            request_id = str(request.get("request_id") or "").strip()
            title = str(request.get("approved_topic") or "").strip()
            if not request_id or not title or request_id in seen_ids:
                continue
            if request.get("approved_by_user") is not True:
                continue
            seen_ids.add(request_id)
            entities.append(
                VideoEntity(
                    video_id=request_id,
                    title=title,
                    durable_state="Ready",
                    request_id=request_id,
                    packet_version=str(request.get("packet_version") or ""),
                    run_id=latest_runs.get(request_id, ""),
                )
            )
    return tuple(entities)


def _filter_snapshot(snapshot: MissionSnapshot, view: str) -> MissionSnapshot:
    if view == "all":
        return snapshot
    target = "Needs Me" if view == "needs" else "Problems" if view == "problems" else None
    if target is None:
        raise ValueError("unsupported Channel OS Telegram view")
    items = tuple(item for item in snapshot.items if item.mission_state == target)
    counts = Counter(item.mission_state for item in items)
    normalized = {name: int(counts.get(name, 0)) for name in MISSION_STATES}
    unavailable = sum(1 for item in items if item.source == "live-source-unavailable")
    return replace(snapshot, items=items, counts=normalized, source_unavailable_count=unavailable)


def render_control_state(
    state: Mapping[str, Any],
    *,
    repository: str,
    github_token: str,
    memory_root: str | Path,
    view: str = "all",
    live_provider: LiveStateProvider | None = None,
) -> tuple[str, list[list[dict[str, str]]]]:
    entities = video_entities_from_control_state(state)
    bindings = {entity.video_id: entity.run_id for entity in entities if entity.run_id}
    provider = live_provider or GitHubActionsLiveStateProvider(repository, bindings, github_token)
    snapshot = MissionControl(ChannelOSMemory(memory_root), provider).snapshot(entities)
    projected = _filter_snapshot(snapshot, view)
    text, keyboard = render_telegram(projected)
    if view == "needs":
        text = "🟠 Needs Me — Channel OS\n\n" + text
    elif view == "problems":
        text = "❌ Problems — Channel OS\n\n" + text
    text += "\n\nقراءة فقط: لا يبدأ هذا العرض Production ولا ينشر إلى YouTube."
    keyboard = list(keyboard) + [[{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}]]
    return text, keyboard
