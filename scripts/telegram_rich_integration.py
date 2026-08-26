from __future__ import annotations

from typing import Any

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_production_rich_ui as rich


_INSTALLED = False


def _release_to_delivery(releases: Any) -> dict[str, Any]:
    try:
        latest = releases.latest() if hasattr(releases, "latest") else None
    except Exception:
        latest = None
    if isinstance(latest, dict):
        return latest
    if isinstance(releases, list) and releases:
        item = releases[-1]
        if isinstance(item, dict):
            return item
    return {}


def _status_payload(state: dict[str, Any], releases: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "stage",
        "phase",
        "status",
        "progress",
        "message",
        "detail",
        "note",
        "title",
        "topic",
        "approved_topic",
        "run_id",
        "request_id",
    ):
        if key in state:
            payload[key] = state.get(key)
    target = state.get(getattr(active, "PRODUCTION_TARGET_KEY", "production_target"))
    if isinstance(target, dict):
        for key in ("request_id", "run_id", "title", "topic", "approved_topic"):
            if not payload.get(key) and target.get(key):
                payload[key] = target.get(key)
    latest = _release_to_delivery(releases)
    if isinstance(latest, dict):
        for key in ("run_id", "request_id", "title", "topic", "approved_topic"):
            if not payload.get(key) and latest.get(key):
                payload[key] = latest.get(key)
    return payload


def _quality_payload(state: dict[str, Any], releases: Any) -> dict[str, Any] | None:
    for key in ("quality_report", "quality_gates", "last_quality_report", "failure_report"):
        value = state.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"gates": value}
    latest = _release_to_delivery(releases)
    if isinstance(latest, dict):
        for key in ("quality_report", "quality_gates", "gates"):
            value = latest.get(key)
            if isinstance(value, dict):
                return value
            if isinstance(value, list):
                return {"gates": value, "title": latest.get("title"), "run_id": latest.get("run_id")}
    return None


def _send_status_rich(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> None:
    payload = _status_payload(state, releases)
    fallback = panel._status_text(state, releases)
    rich.send_rich_with_fallback(client, chat_id, rich.production_status_rich_message(payload), fallback)


def _send_last_delivery_rich(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> None:
    delivery = _release_to_delivery(releases)
    if not delivery:
        handler = getattr(active, "_ISCO_RICH_BASE_HANDLE", None)
        if handler is None:
            raise RuntimeError("Telegram rich integration base handler is not installed")
        handler("last_delivery", client, state, releases, chat_id)
        return
    title = str(delivery.get("title") or delivery.get("topic") or "").strip()
    fallback = "🎁 آخر إنتاج" + (f"\n\n🎯 {title}" if title else "")
    rich.send_rich_with_fallback(client, chat_id, rich.last_delivery_rich_message(delivery), fallback)


def _send_quality_rich_if_present(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> bool:
    report = _quality_payload(state, releases)
    if not report:
        return False
    fallback = "🧪 Quality Gates\n\n" + str(report)
    rich.send_rich_with_fallback(client, chat_id, rich.quality_gates_rich_message(report), fallback)
    return True


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "status":
        _send_status_rich(client, state, releases, chat_id)
        _send_quality_rich_if_present(client, state, releases, chat_id)
        return
    if kind == "last_delivery":
        _send_last_delivery_rich(client, state, releases, chat_id)
        return
    handler = getattr(active, "_ISCO_RICH_BASE_HANDLE", None)
    if handler is None:
        raise RuntimeError("Telegram rich integration base handler is not installed")
    handler(kind, client, state, releases, chat_id)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    if not hasattr(active, "_ISCO_RICH_BASE_HANDLE"):
        active._ISCO_RICH_BASE_HANDLE = active._handle_command
    active._handle_command = _handle_command
    _INSTALLED = True
