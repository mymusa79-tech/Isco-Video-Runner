from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

SEVERITY_INFO = "INFO"
SEVERITY_SUCCESS = "SUCCESS"
SEVERITY_WARNING = "WARNING"
SEVERITY_ACTION = "ACTION"
SEVERITIES = frozenset(
    {
        SEVERITY_INFO,
        SEVERITY_SUCCESS,
        SEVERITY_WARNING,
        SEVERITY_ACTION,
    }
)

STATUS_EMOJI = {
    "running": "🔵",
    "success": "✅",
    "warning": "⚠️",
    "failed": "❌",
    "action_required": "🟠",
    "held": "⏸️",
}

STAGE_LABELS = {
    "planning": "التخطيط",
    "voice": "الصوت",
    "visuals": "المشاهد",
    "mux": "التجميع",
}

ACTION_VIEW_GITHUB = "view_github"
ACTION_VIEW_LOGS = "view_logs"
ACTION_VIEW_RESULTS = "view_results"
ACTION_DETAILS = "details"
ACTION_COMPACT = "compact"
ACTION_APPROVE = "approve"
ACTION_REJECT = "reject"

# Retry is intentionally absent from the V1 action vocabulary. It is a separately
# gated future feature that requires a live control-plane receiver and retry-safe
# classification before it can exist as a functional Telegram action.
V1_ACTIONS = frozenset(
    {
        ACTION_VIEW_GITHUB,
        ACTION_VIEW_LOGS,
        ACTION_VIEW_RESULTS,
        ACTION_DETAILS,
        ACTION_COMPACT,
        ACTION_APPROVE,
        ACTION_REJECT,
    }
)


@dataclass(frozen=True)
class TelegramMessageContract:
    """Presentation-only contract shared by Telegram operations surfaces.

    This object carries no production decision logic. It only describes what a
    Telegram card may render: status/severity/headline/summary/stage/reason/run_id/actions.
    """

    status: str
    severity: str
    headline: str
    summary: str = ""
    stage: str = ""
    reason: str = ""
    run_id: str = ""
    actions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ValueError(f"Unsupported Telegram severity: {self.severity}")
        if not self.status.strip():
            raise ValueError("Telegram status must be non-empty")
        if not self.headline.strip():
            raise ValueError("Telegram headline must be non-empty")
        unknown = [action for action in self.actions if action not in V1_ACTIONS]
        if unknown:
            raise ValueError("Unsupported Telegram V1 actions: " + ", ".join(unknown))


def contract_dict(contract: TelegramMessageContract) -> dict[str, object]:
    """Stable dictionary form used by tests and future renderers."""
    return {
        "status": contract.status,
        "severity": contract.severity,
        "headline": contract.headline,
        "summary": contract.summary,
        "stage": contract.stage,
        "reason": contract.reason,
        "run_id": contract.run_id,
        "actions": list(contract.actions),
    }


def url_button(text: str, url: str) -> dict[str, str]:
    value = str(url or "").strip()
    if not value.startswith(("https://", "http://")):
        raise ValueError("Telegram URL buttons require an absolute http(s) URL")
    return {"text": str(text), "url": value}


def callback_button(text: str, data: str) -> dict[str, str]:
    value = str(data or "")
    if not value:
        raise ValueError("Telegram callback_data must be non-empty")
    if len(value.encode("utf-8")) > 64:
        raise ValueError("Telegram callback_data exceeds the 64-byte Bot API limit")
    return {"text": str(text), "callback_data": value}


def inline_keyboard(rows: Iterable[Iterable[Mapping[str, str]]]) -> dict[str, list[list[dict[str, str]]]]:
    normalized: list[list[dict[str, str]]] = []
    for row in rows:
        buttons = [dict(button) for button in row]
        if buttons:
            normalized.append(buttons)
    return {"inline_keyboard": normalized}
