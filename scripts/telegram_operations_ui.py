from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

SEVERITY_INFO = "INFO"
SEVERITY_SUCCESS = "SUCCESS"
SEVERITY_WARNING = "WARNING"
SEVERITY_ACTION = "ACTION"
SEVERITIES = frozenset({SEVERITY_INFO, SEVERITY_SUCCESS, SEVERITY_WARNING, SEVERITY_ACTION})

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

_OPS_CALLBACK_PREFIXES = {
    ACTION_DETAILS: "opsdetails",
    ACTION_COMPACT: "opscompact",
}


@dataclass(frozen=True)
class TelegramMessageContract:
    """Presentation-only contract shared by Telegram operations surfaces."""

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


def operations_callback_data(action: str, run_id: str, message_id: str) -> str:
    """Bind a presentation-only toggle to one GitHub run and Telegram message."""
    prefix = _OPS_CALLBACK_PREFIXES.get(str(action or ""))
    run_value = str(run_id or "").strip()
    message_value = str(message_id or "").strip()
    if prefix is None:
        raise ValueError("Unsupported Telegram operations callback action")
    if not run_value.isdigit() or not message_value.isdigit():
        raise ValueError("Telegram operations callback requires numeric run/message ids")
    if int(run_value) <= 0 or int(message_value) <= 0:
        raise ValueError("Telegram operations callback ids must be positive")
    data = f"cmd:{prefix}-{run_value}-{message_value}"
    callback_button("x", data)
    return data


def parse_operations_command(kind: str) -> tuple[str, str, str] | None:
    """Parse the command portion after panel.poll has already authorized the callback."""
    value = str(kind or "").strip()
    for action, prefix in _OPS_CALLBACK_PREFIXES.items():
        marker = prefix + "-"
        if not value.startswith(marker):
            continue
        remainder = value[len(marker) :]
        parts = remainder.split("-")
        if len(parts) != 2:
            return None
        run_id, message_id = parts
        if not run_id.isdigit() or not message_id.isdigit():
            return None
        if int(run_id) <= 0 or int(message_id) <= 0:
            return None
        return action, run_id, message_id
    return None


def _run_suffix(run_number: str) -> str:
    value = str(run_number or "").strip()
    return f" · Run #{value}" if value else ""


def render_progress_text(*, run_number: str = "", topic: str = "", current_stage: str | None = None, completed: Iterable[str] = ()) -> str:
    """Render one edit-in-place lifecycle card for an active production run."""
    completed_set = set(completed)
    headline = "بدأ الإنتاج" if current_stage is None and not completed_set else "الإنتاج جارٍ"
    lines = [f"🔵 {headline}{_run_suffix(run_number)}"]
    topic_value = str(topic or "").strip()
    if topic_value:
        lines.extend(["", f"🎬 {topic_value}"])
    parts: list[str] = []
    for key, label in STAGE_LABELS.items():
        if key in completed_set:
            marker = "✅"
        elif key == current_stage:
            marker = "🔵"
        else:
            marker = "⏳"
        parts.append(f"{label} {marker}")
    lines.extend(["", " · ".join(parts)])
    if current_stage is None and not completed_set:
        lines.extend(["", "لا يحتاج أي إجراء منك الآن."])
    return "\n".join(lines)


def render_failure_text(*, run_number: str = "", stage: str = "", duration: str = "", reason: str = "", impact: str = "") -> str:
    """Compact actionable terminal failure card; no retry action is rendered in V1."""
    stage_value = str(stage or "").strip()
    headline = "❌ فشل الإنتاج"
    if stage_value:
        headline += f" · {stage_value}"
    lines = [f"{headline}{_run_suffix(run_number)}"]
    reason_value = str(reason or "").strip() or "راجع تفاصيل GitHub لمعرفة السبب التقني."
    lines.extend(["", "السبب:", reason_value])
    impact_value = str(impact or "").strip()
    if impact_value:
        lines.extend(["", "الأثر:", impact_value])
    duration_value = str(duration or "").strip()
    if duration_value:
        lines.extend(["", f"⏱️ توقف بعد {duration_value}"])
    return "\n".join(lines)


def render_success_text(
    *,
    run_number: str = "",
    topic: str = "",
    bundle_summary: str = "الحزمة النهائية جاهزة",
    duration: str = "",
    warning: str = "",
    quality_passed: bool = True,
) -> str:
    """Render a terminal success card, promoting degraded success to WARNING."""
    warning_value = str(warning or "").strip()
    if warning_value:
        lines = [f"⚠️ الإنتاج مكتمل مع ملاحظة{_run_suffix(run_number)}"]
    else:
        lines = [f"✅ الإنتاج مكتمل{_run_suffix(run_number)}"]
    topic_value = str(topic or "").strip()
    if topic_value:
        lines.extend(["", f"🎬 {topic_value}"])
    summary_value = str(bundle_summary or "").strip() or "الحزمة النهائية جاهزة"
    lines.extend(["", summary_value])
    if warning_value:
        lines.extend(["", "ملاحظة:", warning_value])
    if quality_passed:
        lines.extend(["", "Quality Gates: Passed"])
    duration_value = str(duration or "").strip()
    if duration_value:
        lines.append(f"⏱️ {duration_value}")
    return "\n".join(lines)
