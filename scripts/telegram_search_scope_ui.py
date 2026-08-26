from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from scripts import telegram_control_panel as panel


SEARCH_SCOPE_HINT_KEY = "telegram_search_scope_hint_v1"
ACTIVE_RESEARCH_SESSION_KEY = "active_research_session_id"
_VALID_LONG_SCOPES = {"bundle", "long"}
_BASE_POLL = panel.poll


def pending_long_ids(state: dict[str, Any]) -> set[str]:
    return {
        str(item.get("action_id") or "")
        for item in state.get("pending_actions", [])
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("kind") == "long"
        and str(item.get("action_id") or "")
    }


def bind_scope_to_new_long_request(
    state: dict[str, Any],
    *,
    scope: str | None,
    before_ids: set[str],
) -> bool:
    """Bind scope only when this interaction actually queued a new long search."""
    new_items = [
        item
        for item in state.get("pending_actions", [])
        if isinstance(item, dict)
        and item.get("status") == "pending"
        and item.get("kind") == "long"
        and str(item.get("action_id") or "") not in before_ids
    ]
    if not new_items:
        return False
    if scope in _VALID_LONG_SCOPES:
        state[SEARCH_SCOPE_HINT_KEY] = {
            "schema_version": 1,
            "kind": "long",
            "scope": scope,
            "action_id": str(new_items[-1].get("action_id") or ""),
            "requested_at": panel._now(),
        }
    else:
        # A newly queued legacy long search deliberately restores the old
        # post-selection scope prompt instead of inheriting a previous hint.
        state.pop(SEARCH_SCOPE_HINT_KEY, None)
    return True


def preferred_scope_for_session(state: dict[str, Any], session: dict[str, Any]) -> str | None:
    hint = state.get(SEARCH_SCOPE_HINT_KEY)
    if not isinstance(hint, dict) or hint.get("kind") != "long":
        return None
    scope = str(hint.get("scope") or "")
    if scope not in _VALID_LONG_SCOPES:
        return None
    # Saved ideas must never inherit a stale search-mode choice.
    if session.get("source") == "saved_suggestion":
        return None
    session_id = str(session.get("session_id") or "")
    active_session = str(state.get(ACTIVE_RESEARCH_SESSION_KEY) or "")
    if not session_id or session_id != active_session:
        return None
    return scope


def clear_scope_hint(state: dict[str, Any]) -> None:
    state.pop(SEARCH_SCOPE_HINT_KEY, None)


def _rewrite_update(state: dict[str, Any], update: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate only scoped-search callbacks into the already-certified V2 contract."""
    rewritten = copy.deepcopy(update)
    callback = rewritten.get("callback_query")
    meta: dict[str, Any] = {"long_start": False, "requested_scope": None, "consume_scope": False}
    if not isinstance(callback, dict):
        return rewritten, meta

    data = str(callback.get("data") or "")
    if data in {"cmd:topic_bundle", "cmd:topic_long", "cmd:topic"}:
        meta["long_start"] = True
        if data == "cmd:topic_bundle":
            meta["requested_scope"] = "bundle"
        elif data == "cmd:topic_long":
            meta["requested_scope"] = "long"
        # Reuse the canonical long research command. Scope is bound only after
        # the canonical poll proves that this click actually queued a new action.
        callback["data"] = "cmd:topic"
        return rewritten, meta

    parts = data.split(":")
    if len(parts) == 3 and parts[0] == "pick":
        session = panel._session(state, parts[1])
        if isinstance(session, dict) and session.get("kind") == "long":
            scope = preferred_scope_for_session(state, session)
            if scope:
                callback["data"] = f"scope:{parts[1]}:{parts[2]}:{scope}"
                meta["consume_scope"] = True
        return rewritten, meta

    if len(parts) == 4 and parts[0] == "scope" and parts[3] in _VALID_LONG_SCOPES:
        meta["consume_scope"] = True
    return rewritten, meta


def poll(state_path: Path) -> None:
    """Run the canonical Telegram poll with a narrow callback rewrite layer.

    This deliberately does *not* copy the panel poll implementation. It rewrites
    only the three new search-mode callbacks and scoped candidate selection, then
    delegates authorization, idempotency, menus, approvals and failure behavior
    to the existing certified poll.
    """
    before_state = panel.load_state(state_path)
    before_long_ids = pending_long_ids(before_state)
    rewrite_state = before_state
    original_call = panel.TelegramClient.call
    metas: list[dict[str, Any]] = []

    def scoped_call(self, method: str, payload: dict[str, Any] | None = None):
        result = original_call(self, method, payload)
        if method != "getUpdates" or not isinstance(result, list):
            return result
        rewritten_updates = []
        for item in result:
            if not isinstance(item, dict):
                rewritten_updates.append(item)
                continue
            rewritten, meta = _rewrite_update(rewrite_state, item)
            metas.append(meta)
            rewritten_updates.append(rewritten)
        return rewritten_updates

    panel.TelegramClient.call = scoped_call
    try:
        _BASE_POLL(state_path)
    finally:
        panel.TelegramClient.call = original_call

    state = panel.load_state(state_path)
    first_long_start = next((meta for meta in metas if meta.get("long_start")), None)
    changed = False
    if first_long_start is not None:
        changed = bind_scope_to_new_long_request(
            state,
            scope=first_long_start.get("requested_scope"),
            before_ids=before_long_ids,
        ) or changed
    if any(meta.get("consume_scope") for meta in metas):
        if SEARCH_SCOPE_HINT_KEY in state:
            clear_scope_hint(state)
            changed = True
    if changed:
        state["last_event_at"] = panel._now()
        panel.save_state(state_path, state)
