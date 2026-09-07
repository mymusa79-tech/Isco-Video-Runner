from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable


VALID_LONG_SCOPES = frozenset({"bundle", "long"})
_SCOPE_COMMANDS = {
    "topic_bundle": "bundle",
    "topic_long": "long",
}


def _scope(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized if normalized in VALID_LONG_SCOPES else None


def pending_long_ids(state: dict[str, Any]) -> set[str]:
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return set()
    return {
        str(item.get("action_id") or "")
        for item in actions
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("kind") == "long"
        and str(item.get("action_id") or "")
    }


def bind_scope_to_new_long_request(
    state: dict[str, Any],
    *,
    requested_scope: str,
    before_ids: set[str],
) -> bool:
    """Bind scope only to a long request created by this exact interaction.

    An already-pending long research request is immutable from a later scope click.
    This prevents a UI retry from silently changing editorial intent underneath an
    action that may already be claimed by the scheduler.
    """
    scope = _scope(requested_scope)
    if scope is None:
        return False
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return False
    new_items = [
        item
        for item in actions
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("kind") == "long"
        and str(item.get("action_id") or "")
        and str(item.get("action_id") or "") not in before_ids
    ]
    if not new_items:
        return False
    new_items[-1]["requested_scope"] = scope
    return True


def scope_for_session(session: object) -> str | None:
    if not isinstance(session, dict) or str(session.get("kind") or "") != "long":
        return None
    # Saved suggestions deliberately contain no requested_scope. They retain the
    # legacy post-pick scope question and can never inherit a prior search choice.
    return _scope(session.get("requested_scope"))


def _scoped_search_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🎬➕⚡ حلقة + Shorts", "callback_data": "cmd:topic_bundle"}],
        [{"text": "🎬 حلقة فقط", "callback_data": "cmd:topic_long"}],
        [{"text": "⚡ Short فقط", "callback_data": "cmd:short"}],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _scoped_search_text() -> str:
    return (
        "🔎 بحث جديد\n\n"
        "اختر النتيجة التي تريدها قبل البحث:\n"
        "🎬➕⚡ حلقة + Shorts — الفكرة المختارة تُعتمد مع 2–3 Shorts مختلفة حسب المادة.\n"
        "🎬 حلقة فقط — الفكرة المختارة تُعتمد كحلقة فقط.\n"
        "⚡ Short فقط — بحث Short مستقل.\n\n"
        "هذا يحدد نطاق القرار فقط؛ لا يبدأ Production."
    )


def _rewrite_refresh_rows(rows: object, *, requested_scope: str | None) -> object:
    scope = _scope(requested_scope)
    if scope is None or not isinstance(rows, list):
        return rows
    callback = "cmd:topic_bundle" if scope == "bundle" else "cmd:topic_long"
    copied = copy.deepcopy(rows)
    for row in copied:
        if not isinstance(row, list):
            continue
        for button in row:
            if isinstance(button, dict) and button.get("callback_data") == "refresh:long":
                button["callback_data"] = callback
    return copied


def _rewrite_scoped_picks(panel, state: dict[str, Any], updates: object) -> object:
    if not isinstance(updates, list):
        return updates
    rewritten = copy.deepcopy(updates)
    for update in rewritten:
        if not isinstance(update, dict):
            continue
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            continue
        data = str(callback.get("data") or "")
        parts = data.split(":")
        if len(parts) != 3 or parts[0] != "pick":
            continue
        session = panel._session(state, parts[1])
        scope = scope_for_session(session)
        if scope is not None:
            callback["data"] = f"scope:{parts[1]}:{parts[2]}:{scope}"
    return rewritten


def _install_handler(panel, creator_v5) -> None:
    base_handler = panel._handle_command

    def handle(kind, client, state, releases, chat_id):
        if kind in _SCOPE_COMMANDS:
            before = pending_long_ids(state)
            base_handler("topic", client, state, releases, chat_id)
            bind_scope_to_new_long_request(
                state,
                requested_scope=_SCOPE_COMMANDS[kind],
                before_ids=before,
            )
            return
        if isinstance(kind, str) and kind.startswith("choices-"):
            session_id = kind.removeprefix("choices-").strip()
            session = panel._session(state, session_id)
            scope = scope_for_session(session)
            if scope is not None:
                base_keyboard = panel._candidate_keyboard

                def scoped_keyboard(candidate_session_id: str, candidate_kind: str):
                    rows = base_keyboard(candidate_session_id, candidate_kind)
                    if candidate_session_id == session_id and candidate_kind == "long":
                        return _rewrite_refresh_rows(rows, requested_scope=scope)
                    return rows

                panel._candidate_keyboard = scoped_keyboard
                try:
                    return base_handler(kind, client, state, releases, chat_id)
                finally:
                    panel._candidate_keyboard = base_keyboard
        return base_handler(kind, client, state, releases, chat_id)

    panel._handle_command = handle
    creator_v5._search_keyboard = _scoped_search_keyboard
    creator_v5._search_text = _scoped_search_text


def _install_poll(panel) -> None:
    base_poll = panel.poll

    def poll(state_path: Path) -> None:
        state = panel.load_state(state_path)
        base_call = panel.TelegramClient.call

        def call(self, method: str, payload: dict[str, Any] | None = None):
            result = base_call(self, method, payload)
            if method == "getUpdates":
                return _rewrite_scoped_picks(panel, state, result)
            return result

        panel.TelegramClient.call = call
        try:
            base_poll(state_path)
        finally:
            panel.TelegramClient.call = base_call

    panel.poll = poll


def _install_research(panel, research_core) -> None:
    if research_core is None:
        return
    base_research = panel.research

    def research(state_path: Path) -> None:
        before = panel.load_state(state_path)
        pending = next(
            (
                item
                for item in before.get("pending_actions", [])
                if isinstance(item, dict) and item.get("status") == "pending"
            ),
            None,
        )
        scope = None
        if isinstance(pending, dict) and pending.get("kind") == "long":
            scope = _scope(pending.get("requested_scope"))
        if scope is None:
            base_research(state_path)
            return

        base_keyboard = research_core._candidate_keyboard

        def candidate_keyboard(session_id: str, kind: str, count: int):
            rows = base_keyboard(session_id, kind, count)
            if kind == "long":
                return _rewrite_refresh_rows(rows, requested_scope=scope)
            return rows

        research_core._candidate_keyboard = candidate_keyboard
        try:
            base_research(state_path)
        finally:
            research_core._candidate_keyboard = base_keyboard

        after = panel.load_state(state_path)
        active_id = str(after.get("active_research_session_id") or "").strip()
        sessions = after.get("sessions")
        session = sessions.get(active_id) if active_id and isinstance(sessions, dict) else None
        if isinstance(session, dict) and session.get("kind") == "long":
            session["requested_scope"] = scope
            panel.save_state(state_path, after)

    panel.research = research


def install(*, panel, creator_v5, research_core=None) -> None:
    """Install scope-first UX on top of the final modern Telegram stack.

    The module does not own approval or Production authority. It only binds an
    operator-selected Long scope to one research action/session and translates the
    later candidate pick into the already-certified canonical ``scope:`` callback.
    """
    if getattr(panel, "_isco_scope_first_modern_installed", False):
        return
    panel._isco_scope_first_modern_installed = True
    _install_handler(panel, creator_v5)
    _install_poll(panel)
    _install_research(panel, research_core)
