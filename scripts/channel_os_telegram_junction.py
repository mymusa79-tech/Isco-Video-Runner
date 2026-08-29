from __future__ import annotations

"""Pure Channel OS V1 <-> Telegram L6 junction.

Channel OS owns domain decisions and rendering only. This module deliberately owns
no Telegram credential, HTTP client, polling loop, GitHub dispatch, retry policy,
or publication authority. It translates authenticated Channel OS callback intents
and Mission Control snapshots into the request shape accepted by the L6 durable
Telegram outbox. The caller remains responsible for supplying a truthful
MissionSnapshot from the Channel OS domain and for dispatching the resulting intent
through the L6 outbox workflow.
"""

from collections import Counter
from enum import Enum
import hashlib
import json
from typing import Mapping

from scripts.channel_os_mission_control import MISSION_STATES, MissionSnapshot, render_telegram


class ChannelOSTelegramContractError(ValueError):
    pass


class ChannelOSTelegramCommand(str, Enum):
    REFRESH = "refresh"
    NEEDS_ME = "needs"
    PROBLEMS = "problems"


_CALLBACKS: Mapping[str, ChannelOSTelegramCommand] = {
    "cmd:channelos-refresh": ChannelOSTelegramCommand.REFRESH,
    "cmd:channelos-needs": ChannelOSTelegramCommand.NEEDS_ME,
    "cmd:channelos-problems": ChannelOSTelegramCommand.PROBLEMS,
}


def parse_channel_os_callback(callback_data: str) -> ChannelOSTelegramCommand:
    value = str(callback_data or "").strip()
    try:
        return _CALLBACKS[value]
    except KeyError as exc:
        raise ChannelOSTelegramContractError("unsupported Channel OS Telegram callback") from exc


def is_channel_os_callback(callback_data: str) -> bool:
    return str(callback_data or "").strip() in _CALLBACKS


def _filtered_snapshot(snapshot: MissionSnapshot, command: ChannelOSTelegramCommand) -> MissionSnapshot:
    if command is ChannelOSTelegramCommand.REFRESH:
        return snapshot
    wanted = "Needs Me" if command is ChannelOSTelegramCommand.NEEDS_ME else "Problems"
    items = tuple(item for item in snapshot.items if item.mission_state == wanted)
    counts = Counter(item.mission_state for item in items)
    normalized = {state: int(counts.get(state, 0)) for state in MISSION_STATES}
    unavailable = sum(1 for item in items if item.source == "live-source-unavailable")
    return MissionSnapshot(
        items=items,
        counts=normalized,
        observed_at=snapshot.observed_at,
        source_unavailable_count=unavailable,
    )


def _snapshot_digest(snapshot: MissionSnapshot, command: ChannelOSTelegramCommand) -> str:
    payload = {
        "authority": "projection_only",
        "command": command.value,
        "observed_at": snapshot.observed_at,
        "source_unavailable_count": snapshot.source_unavailable_count,
        "counts": {state: int(snapshot.counts.get(state, 0)) for state in MISSION_STATES},
        "items": [
            {
                "video_id": item.video_id,
                "title": item.title,
                "mission_state": item.mission_state,
                "run_id": item.run_id,
                "source": item.source,
                "observed_at": item.observed_at,
                "reason": item.reason,
            }
            for item in snapshot.items
        ],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_l6_outbox_intent(
    snapshot: MissionSnapshot,
    *,
    command: ChannelOSTelegramCommand,
    interaction_id: str,
    max_items: int = 12,
) -> dict[str, object]:
    """Return an L6 outbox request without sending or dispatching anything.

    `interaction_id` must identify the already-authenticated webhook interaction
    (normally the Telegram update id/callback id at the Telegram adapter boundary).
    Replaying the same interaction against the same snapshot therefore yields the
    same outbox_message_id, while a later explicit click can produce a fresh message.
    """
    interaction = str(interaction_id or "").strip()
    if not interaction:
        raise ChannelOSTelegramContractError("interaction_id must be non-empty")
    if not isinstance(max_items, int) or isinstance(max_items, bool) or not 1 <= max_items <= 50:
        raise ChannelOSTelegramContractError("max_items must be between 1 and 50")

    selected = _filtered_snapshot(snapshot, command)
    text, keyboard = render_telegram(selected, max_items=max_items)
    digest = _snapshot_digest(selected, command)
    identity_raw = json.dumps(
        {"interaction_id": interaction, "snapshot_digest": digest, "command": command.value},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    identity = hashlib.sha256(identity_raw).hexdigest()

    return {
        "schema_version": 1,
        "outbox_message_id": f"channel-os-{identity}",
        "message_kind": "channel_os_mission_control",
        "correlation_id": f"channel-os:{interaction}",
        "journal_event_ref": f"projection:channel-os:{digest}",
        "created_at": selected.observed_at,
        "method": "sendMessage",
        "payload": {
            "text": text,
            "reply_markup": {"inline_keyboard": keyboard},
        },
        "authority": "projection_only",
        "transport_owner": "l6_telegram_outbox",
        "publication_authority": "none",
    }
