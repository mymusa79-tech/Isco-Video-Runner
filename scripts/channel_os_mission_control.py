from __future__ import annotations

import json
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from scripts.channel_os_memory import ChannelOSMemory, LiveState, LiveStateProvider

MISSION_STATES = ("Ideas", "Ready", "Producing", "Needs Me", "Scheduled", "Published", "Problems")
DURABLE_STATES = {"Ideas", "Ready", "Scheduled", "Published"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VideoEntity:
    video_id: str
    title: str
    durable_state: str
    request_id: str = ""
    packet_version: str = ""
    run_id: str = ""
    publish_target: str = ""

    def __post_init__(self) -> None:
        if not str(self.video_id).strip():
            raise ValueError("video_id must be non-empty")
        if self.durable_state not in DURABLE_STATES:
            raise ValueError("durable_state must be Ideas, Ready, Scheduled or Published")


@dataclass(frozen=True)
class MissionItem:
    video_id: str
    title: str
    mission_state: str
    run_id: str
    source: str
    observed_at: str
    reason: str = ""


@dataclass(frozen=True)
class MissionSnapshot:
    items: tuple[MissionItem, ...]
    counts: Mapping[str, int]
    observed_at: str
    source_unavailable_count: int


class GitHubActionsLiveStateProvider(LiveStateProvider):
    """Read exact GitHub Actions runs by immutable video->run binding.

    The binding is metadata only. Every status read calls GitHub; no prior run state is
    persisted or used as a fallback.
    """

    def __init__(self, repository: str, run_bindings: Mapping[str, str], token: str = "") -> None:
        self.repository = str(repository or "").strip()
        self.run_bindings = {str(k): str(v) for k, v in run_bindings.items()}
        self.token = str(token or "").strip()
        if "/" not in self.repository:
            raise ValueError("repository must be owner/name")

    def fetch(self, video_id: str) -> LiveState:
        run_id = str(self.run_bindings.get(video_id) or "").strip()
        if not run_id.isdigit():
            raise RuntimeError("no exact GitHub Actions run binding for video")
        url = f"https://api.github.com/repos/{self.repository}/actions/runs/{run_id}"
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "isco-channel-os-v1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or str(payload.get("id") or "") != run_id:
            raise RuntimeError("GitHub Actions returned a mismatched run")
        status = str(payload.get("status") or "").strip().lower()
        conclusion = str(payload.get("conclusion") or "").strip().lower()
        if status != "completed":
            logical = "running"
        elif conclusion == "success":
            logical = "success"
        elif conclusion in {"action_required", "neutral"}:
            logical = "action_required"
        else:
            logical = "failed"
        return LiveState(
            video_id=video_id,
            status=logical,
            stage=str(payload.get("name") or ""),
            run_id=run_id,
            source="github-actions",
            observed_at=str(payload.get("updated_at") or _now()),
            available=True,
            reason=conclusion if logical in {"failed", "action_required"} else "",
        )


class MissionControl:
    def __init__(self, memory: ChannelOSMemory, live_provider: LiveStateProvider) -> None:
        self.memory = memory
        self.live_provider = live_provider

    @staticmethod
    def _project(entity: VideoEntity, live: LiveState | None) -> MissionItem:
        if not entity.run_id:
            return MissionItem(
                video_id=entity.video_id,
                title=entity.title,
                mission_state=entity.durable_state,
                run_id="",
                source="durable-entity-metadata",
                observed_at=_now(),
            )
        if live is None or not live.available:
            return MissionItem(
                video_id=entity.video_id,
                title=entity.title,
                mission_state="Problems",
                run_id=entity.run_id,
                source="live-source-unavailable",
                observed_at=(live.observed_at if live else _now()),
                reason=(live.reason if live else "live source unavailable"),
            )
        if live.status == "running":
            projected = "Producing"
        elif live.status == "failed":
            projected = "Problems"
        elif live.status == "action_required":
            projected = "Needs Me"
        elif live.status == "success":
            projected = entity.durable_state if entity.durable_state in {"Scheduled", "Published"} else "Ready"
        else:
            projected = "Problems"
        return MissionItem(
            video_id=entity.video_id,
            title=entity.title,
            mission_state=projected,
            run_id=live.run_id,
            source=live.source,
            observed_at=live.observed_at,
            reason=live.reason,
        )

    def snapshot(self, entities: Iterable[VideoEntity]) -> MissionSnapshot:
        items: list[MissionItem] = []
        unavailable = 0
        for entity in entities:
            live = None
            if entity.run_id:
                live = self.memory.read_live_state(self.live_provider, entity.video_id)
                if not live.available:
                    unavailable += 1
            items.append(self._project(entity, live))
        counts = Counter(item.mission_state for item in items)
        normalized = {name: int(counts.get(name, 0)) for name in MISSION_STATES}
        return MissionSnapshot(tuple(items), normalized, _now(), unavailable)


def render_telegram(snapshot: MissionSnapshot, *, max_items: int = 12) -> tuple[str, list[list[dict[str, str]]]]:
    lines = ["🧭 Isco Channel OS — Mission Control", ""]
    lines.append(" | ".join(f"{state}: {snapshot.counts.get(state, 0)}" for state in MISSION_STATES))
    if snapshot.source_unavailable_count:
        lines.extend(["", f"⚠️ Live State unavailable for {snapshot.source_unavailable_count} item(s); no cached status was substituted."])
    selected = sorted(
        snapshot.items,
        key=lambda item: (MISSION_STATES.index(item.mission_state), item.title.casefold(), item.video_id),
    )[:max_items]
    if selected:
        lines.extend(["", "العمل الحالي:"])
        icons = {"Ideas":"💡","Ready":"🟢","Producing":"🔵","Needs Me":"🟠","Scheduled":"🗓️","Published":"✅","Problems":"❌"}
        for item in selected:
            label = item.title.strip() or item.video_id
            suffix = f" · Run {item.run_id}" if item.run_id else ""
            lines.append(f"{icons[item.mission_state]} {item.mission_state} — {label}{suffix}")
            if item.reason and item.mission_state in {"Problems", "Needs Me"}:
                lines.append(f"   السبب: {item.reason[:180]}")
    lines.extend(["", f"آخر تحقق حي: {snapshot.observed_at}"])
    keyboard = [
        [
            {"text": "🟠 Needs Me", "callback_data": "cmd:channelos-needs"},
            {"text": "❌ Problems", "callback_data": "cmd:channelos-problems"},
        ],
        [{"text": "🔄 تحديث حي", "callback_data": "cmd:channelos-refresh"}],
    ]
    return "\n".join(lines), keyboard
