from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts import telegram_control_panel as panel


SEARCH_SCOPE_HINT_KEY = "telegram_search_scope_hint_v1"
ACTIVE_RESEARCH_SESSION_KEY = "active_research_session_id"
_VALID_LONG_SCOPES = {"bundle", "long"}


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


def _current_long_scope_hint(state: dict[str, Any]) -> str | None:
    hint = state.get(SEARCH_SCOPE_HINT_KEY)
    if not isinstance(hint, dict):
        return None
    scope = str(hint.get("scope") or "")
    return scope if scope in _VALID_LONG_SCOPES else None


def clear_scope_hint(state: dict[str, Any]) -> None:
    state.pop(SEARCH_SCOPE_HINT_KEY, None)


def poll(state_path: Path) -> None:
    """Webhook-aware Telegram poll with preselected long-form scope support.

    The webhook replay path historically called the base panel poll without first
    installing the full Active/Persistent policy onto ``panel``. This poll routes
    commands and approvals through the live Active module explicitly, preserving
    current-target, saved-topic, exact-confirmation and production boundaries.
    """
    from scripts import telegram_control_active_ui as active

    state = panel.load_state(state_path)
    token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE")
    allowed_text = panel._read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE")
    allowed_chat = panel._read_secret_file("TELEGRAM_CHAT_ID_FILE")
    if not token or not allowed_text:
        print("Telegram editorial control disabled: bot token or allowed user is missing")
        panel._github_output("needs_engine", "false")
        panel.save_state(state_path, state)
        return
    try:
        allowed_user = int(allowed_text)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID_FILE is invalid") from exc

    client = panel.TelegramClient(token)
    repository = (os.environ.get("GITHUB_REPOSITORY") or "mymusa79-tech/Isco-Video-Runner").strip()
    releases = panel.GitHubReleaseClient(repository, (os.environ.get("GITHUB_TOKEN") or "").strip())
    offset = int(state.get("telegram_offset", 0) or 0)
    updates = client.call("getUpdates", {"offset": offset, "timeout": 0, "allowed_updates": ["message", "callback_query"]}) or []
    if not isinstance(updates, list):
        updates = []

    for update in updates:
        if not isinstance(update, dict) or "update_id" not in update:
            continue
        state["telegram_offset"] = max(int(state.get("telegram_offset", 0) or 0), int(update["update_id"]) + 1)
        authorized, chat_id, _ = panel._authorized_user(update, allowed_user, allowed_chat)
        callback = update.get("callback_query")
        if not authorized:
            if isinstance(callback, dict) and callback.get("id"):
                try:
                    client.answer_callback(str(callback["id"]), "غير مصرح")
                except Exception:
                    pass
            continue
        if chat_id is None:
            continue

        if isinstance(callback, dict):
            callback_id = str(callback.get("id") or "")
            data = str(callback.get("data") or "")
            if callback_id:
                client.answer_callback(callback_id)
            parts = data.split(":")
            if len(parts) >= 2 and parts[0] == "cmd":
                active._handle_command(parts[1], client, state, releases, chat_id)
            elif len(parts) == 2 and parts[0] == "refresh" and parts[1] in {"long", "short"}:
                if parts[1] == "short":
                    refresh_kind = "short"
                else:
                    scope = _current_long_scope_hint(state)
                    refresh_kind = "topic_bundle" if scope == "bundle" else "topic_long" if scope == "long" else "topic"
                active._handle_command(refresh_kind, client, state, releases, chat_id)
            elif len(parts) == 2 and parts[0] == "pack":
                panel._show_packaging(client, releases, chat_id, parts[1])
            elif len(parts) == 3 and parts[0] == "detail":
                session = panel._session(state, parts[1])
                if not session:
                    client.send(chat_id, "انتهت صلاحية هذه الخيارات. اطلب 3 مواضيع جديدة.", keyboard=active._main_keyboard())
                    continue
                try:
                    index = int(parts[2])
                    item = session["candidates"][index]
                except Exception:
                    client.send(chat_id, "الخيار غير صالح.", keyboard=active._main_keyboard())
                    continue
                pick = "pickshort" if session.get("kind") == "short" else "pick"
                client.send(
                    chat_id,
                    panel._candidate_detail(item, index),
                    keyboard=[
                        [{"text": f"✅ اختيار {index + 1}", "callback_data": f"{pick}:{parts[1]}:{index}"}],
                        [{"text": "↩️ الخيارات", "callback_data": "cmd:menu"}],
                    ],
                )
            elif len(parts) == 3 and parts[0] == "pickshort":
                session = panel._session(state, parts[1])
                if not session or session.get("kind") != "short":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=active._main_keyboard())
                    continue
                request = active._approve_current(state, session, int(parts[2]), "short")
                client.send(chat_id, active._approval_text(request), keyboard=active._main_keyboard())
            elif len(parts) == 3 and parts[0] == "pick":
                session = panel._session(state, parts[1])
                if not session or session.get("kind") != "long":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=active._main_keyboard())
                    continue
                index = int(parts[2])
                candidate = session["candidates"][index]
                preferred_scope = preferred_scope_for_session(state, session)
                if preferred_scope:
                    request = active._approve_current(state, session, index, preferred_scope)
                    clear_scope_hint(state)
                    client.send(chat_id, active._approval_text(request), keyboard=active._main_keyboard())
                else:
                    client.send(
                        chat_id,
                        f"اختر نطاق الإنتاج لهذا الموضوع:\n\n{candidate.get('title', '')}",
                        keyboard=[
                            [{"text": "🎬 حلقة + Shorts", "callback_data": f"scope:{parts[1]}:{index}:bundle"}],
                            [{"text": "🎬 حلقة فقط", "callback_data": f"scope:{parts[1]}:{index}:long"}],
                            [{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}],
                        ],
                    )
            elif len(parts) == 4 and parts[0] == "scope" and parts[3] in _VALID_LONG_SCOPES:
                session = panel._session(state, parts[1])
                if not session or session.get("kind") != "long":
                    client.send(chat_id, "انتهت صلاحية هذا الاختيار.", keyboard=active._main_keyboard())
                    continue
                request = active._approve_current(state, session, int(parts[2]), parts[3])
                clear_scope_hint(state)
                client.send(chat_id, active._approval_text(request), keyboard=active._main_keyboard())
            else:
                client.send(chat_id, active._menu_text(), keyboard=active._main_keyboard())
        else:
            message = update.get("message") or {}
            command = active._command_kind(str(message.get("text") or ""))
            active._handle_command(command or "menu", client, state, releases, chat_id)
        state["last_event_at"] = panel._now()

    panel.save_state(state_path, state)
    needs_engine = any(
        isinstance(item, dict) and item.get("status") == "pending"
        for item in state.get("pending_actions", [])
    )
    panel._github_output("needs_engine", "true" if needs_engine else "false")
