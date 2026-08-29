from __future__ import annotations

import os
import tempfile
from typing import Any, Mapping

from scripts import telegram_control_active_ui as active
from scripts.channel_os_memory import ChannelOSMemory, LiveState
from scripts.channel_os_mission_control import (
    GitHubActionsLiveStateProvider,
    MissionControl,
    MissionSnapshot,
    VideoEntity,
    render_telegram,
)

_INSTALLED = False
_COMMAND_FILTERS = {
    "channelos-refresh": None,
    "channelos-needs": "Needs Me",
    "channelos-problems": "Problems",
}


class _UnavailableLiveStateProvider:
    """Fail-closed provider used when GitHub identity is unavailable."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "GitHub live source unavailable")

    def fetch(self, video_id: str) -> LiveState:
        return LiveState.unavailable(video_id, self.reason)


def _latest_queue_actions(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        return {}
    latest: dict[str, tuple[tuple[str, int, int], Mapping[str, Any]]] = {}
    for index, item in enumerate(queue):
        if not isinstance(item, Mapping):
            continue
        request_id = str(item.get("request_id") or "").strip()
        if not request_id:
            continue
        timestamp = str(
            item.get("consumed_at")
            or item.get("reserved_at")
            or item.get("requested_at")
            or ""
        )
        try:
            attempt = int(item.get("attempt", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        rank = (timestamp, attempt, index)
        previous = latest.get(request_id)
        if previous is None or rank > previous[0]:
            latest[request_id] = (rank, item)
    return {request_id: value[1] for request_id, value in latest.items()}


def entities_from_control_state(state: Mapping[str, Any]) -> tuple[VideoEntity, ...]:
    """Project approved Telegram requests into Channel OS without creating new truth.

    Production completion is deliberately *not* treated as YouTube publication. Until
    a separate durable publication source exists, approved requests stay `Ready`
    unless their exact latest production run projects a live transient state.
    """

    requests = state.get("requests")
    if not isinstance(requests, Mapping):
        return ()
    latest_actions = _latest_queue_actions(state)
    entities: list[VideoEntity] = []
    for key, request in requests.items():
        if not isinstance(request, Mapping) or request.get("approved_by_user") is not True:
            continue
        request_id = str(request.get("request_id") or key or "").strip()
        title = str(request.get("approved_topic") or "").strip()
        if not request_id or not title:
            continue
        action = latest_actions.get(request_id)
        run_id = ""
        if isinstance(action, Mapping):
            candidate = str(action.get("workflow_run_id") or "").strip()
            if candidate.isdigit():
                run_id = candidate
        entities.append(
            VideoEntity(
                video_id=request_id,
                title=title,
                durable_state="Ready",
                request_id=request_id,
                run_id=run_id,
            )
        )
    return tuple(sorted(entities, key=lambda item: (item.title.casefold(), item.video_id)))


def _filter_snapshot(snapshot: MissionSnapshot, mission_state: str | None) -> MissionSnapshot:
    if mission_state is None:
        return snapshot
    items = tuple(item for item in snapshot.items if item.mission_state == mission_state)
    unavailable = sum(1 for item in items if item.source == "live-source-unavailable")
    return MissionSnapshot(
        items=items,
        counts=snapshot.counts,
        observed_at=snapshot.observed_at,
        source_unavailable_count=unavailable,
    )


def render_from_control_state(
    state: Mapping[str, Any],
    *,
    repository: str,
    token: str = "",
    mission_state: str | None = None,
) -> tuple[str, list[list[dict[str, str]]]]:
    entities = entities_from_control_state(state)
    run_bindings = {item.video_id: item.run_id for item in entities if item.run_id}
    repository = str(repository or "").strip()
    if "/" in repository:
        provider = GitHubActionsLiveStateProvider(repository, run_bindings, token=token)
    else:
        provider = _UnavailableLiveStateProvider("GitHub repository identity is unavailable")

    # The Telegram projection must not become a fifth persistent Channel OS memory
    # store. A temporary empty memory boundary is used only for its fail-closed live
    # state read contract and is discarded after every request.
    with tempfile.TemporaryDirectory(prefix="isco-channel-os-view-") as root:
        snapshot = MissionControl(ChannelOSMemory(root), provider).snapshot(entities)
    projected = _filter_snapshot(snapshot, mission_state)
    text, keyboard = render_telegram(projected)
    if mission_state and not projected.items:
        text += f"\n\nلا توجد عناصر في حالة {mission_state} الآن."
    return text, keyboard


def _handle_command_with_channel_os(kind, client, state, releases, chat_id) -> None:
    if kind in _COMMAND_FILTERS:
        repository = str(
            getattr(releases, "repository", "")
            or os.environ.get("GITHUB_REPOSITORY")
            or ""
        ).strip()
        token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
        text, keyboard = render_from_control_state(
            state,
            repository=repository,
            token=token,
            mission_state=_COMMAND_FILTERS[kind],
        )
        client.send(chat_id, text, keyboard=keyboard)
        return
    handler = getattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE", None)
    if handler is None:
        raise RuntimeError("Channel OS Telegram base handler is not installed")
    handler(kind, client, state, releases, chat_id)


def _main_keyboard_with_channel_os() -> list[list[dict[str, str]]]:
    keyboard = getattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD", None)
    if keyboard is None:
        raise RuntimeError("Channel OS Telegram base keyboard is not installed")
    rows = [[dict(button) for button in row] for row in keyboard()]
    if any(
        button.get("callback_data") == "cmd:channelos-refresh"
        for row in rows
        for button in row
        if isinstance(button, dict)
    ):
        return rows
    rows.append([{"text": "🧭 Channel OS", "callback_data": "cmd:channelos-refresh"}])
    return rows


def install() -> None:
    """Install the read-only Channel OS surface into the existing Telegram chain."""

    global _INSTALLED
    if _INSTALLED:
        return
    if not hasattr(active, "_ISCO_CHANNEL_OS_BASE_HANDLE"):
        active._ISCO_CHANNEL_OS_BASE_HANDLE = active._handle_command
    if not hasattr(active, "_ISCO_CHANNEL_OS_BASE_KEYBOARD"):
        active._ISCO_CHANNEL_OS_BASE_KEYBOARD = active._main_keyboard
    active._handle_command = _handle_command_with_channel_os
    active._main_keyboard = _main_keyboard_with_channel_os
    _INSTALLED = True
