from __future__ import annotations

from typing import Any

from scripts import telegram_control_active_ui as ui
from scripts import telegram_control_panel as panel


def _is_used_topic_globally(state: dict[str, Any], kind: str, title: str) -> bool:
    del kind  # A completed topic is blocked across long and short research.
    return any(
        ui._same_topic_title(title, str(item.get("topic") or ""))
        for item in ui._used_topics(state)
    )


def _approve_without_consuming_saved(
    state: dict[str, Any], session: dict[str, Any], index: int, scope: str
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "").strip()
    active_session = str(state.get(ui.ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not session_id or session_id != active_session:
        raise RuntimeError("This Telegram selection is not from the current research session")
    request = ui.simple._approve(state, session, index, scope)
    # Keep the idea in Saved until Production actually succeeds. The production
    # workflow moves/removes it only after the deterministic delivery Release exists.
    state[ui.PRODUCTION_TARGET_KEY] = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": str(request.get("request_sha256") or ""),
        "session_id": session_id,
        "selected_at": str(request.get("approved_at") or panel._now()),
    }
    return request


def _menu_text_global_history() -> str:
    return ui._ORIGINAL_MENU_TEXT().replace(
        "ولا يعود في بحث النوع نفسه.",
        "ولا يعود في أي بحث جديد، حلقة أو شورت.",
    )


def _used_page_global_history(state: dict[str, Any], page: int):
    text, keyboard = ui._ORIGINAL_USED_PAGE(state, page)
    return text.replace(
        "وتمنع إعادة الموضوع في بحث النوع نفسه.",
        "وتمنع إعادة الموضوع في أي بحث جديد، حلقة أو شورت.",
    ), keyboard


def _install_policy() -> None:
    if not hasattr(ui, "_ORIGINAL_MENU_TEXT"):
        ui._ORIGINAL_MENU_TEXT = ui._menu_text
    if not hasattr(ui, "_ORIGINAL_USED_PAGE"):
        ui._ORIGINAL_USED_PAGE = ui._used_page
    ui._is_used_topic = _is_used_topic_globally
    ui._approve_current = _approve_without_consuming_saved
    ui._menu_text = _menu_text_global_history
    ui._used_page = _used_page_global_history


def main() -> None:
    _install_policy()
    ui.main()


if __name__ == "__main__":
    main()
