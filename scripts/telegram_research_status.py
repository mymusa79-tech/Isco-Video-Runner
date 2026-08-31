from __future__ import annotations

from typing import Any


def pending_research(state: dict[str, Any], kind: str | None = None) -> dict[str, Any] | None:
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return None
    for item in actions:
        if not isinstance(item, dict) or item.get("status") != "pending":
            continue
        if kind is not None and str(item.get("kind") or "long") != kind:
            continue
        return item
    return None


def live_pending_count(state: dict[str, Any]) -> int:
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return 0
    return sum(1 for item in actions if isinstance(item, dict) and item.get("status") == "pending")


def _message_id(result: Any) -> int | None:
    if not isinstance(result, dict):
        return None
    try:
        value = int(result.get("message_id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def bind_message(action: dict[str, Any], result: Any, *, chat_id: int | str) -> int | None:
    message_id = _message_id(result)
    if message_id is None:
        return None
    action["status_message_id"] = message_id
    action["status_chat_id"] = str(chat_id)
    return message_id


def _edit_payload(chat_id: int | str, message_id: int, text: str, keyboard) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": int(message_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if keyboard is not None:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return payload


def _definitely_missing_message(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "message to edit not found",
            "message can't be edited",
            "message can\'t be edited",
            "message identifier is not specified",
        )
    )


def update_message(
    client,
    action: dict[str, Any],
    text: str,
    *,
    keyboard=None,
    chat_id: int | str | None = None,
    allow_create_if_missing: bool = True,
):
    """Update one durable research card without blind duplicate sends.

    If a prior message identity exists, edit it in place. Only a deterministic
    Telegram "message missing/not editable" outcome may create a replacement.
    Network/unknown failures are propagated so callers never send a duplicate after
    an ambiguous edit outcome.
    """
    target_chat = chat_id if chat_id is not None else action.get("status_chat_id") or action.get("chat_id")
    if target_chat is None:
        raise RuntimeError("Research status card has no chat id")
    try:
        message_id = int(action.get("status_message_id") or 0)
    except (TypeError, ValueError):
        message_id = 0
    if message_id > 0:
        try:
            return client.call("editMessageText", _edit_payload(target_chat, message_id, text, keyboard))
        except Exception as exc:
            if "message is not modified" in str(exc).casefold():
                return {"message_id": message_id}
            if not (allow_create_if_missing and _definitely_missing_message(exc)):
                raise
            action.pop("status_message_id", None)
            action.pop("status_chat_id", None)
    if not allow_create_if_missing:
        raise RuntimeError("Research status message identity is unavailable")
    result = client.send(target_chat, text, keyboard=keyboard)
    bind_message(action, result, chat_id=target_chat)
    return result


def prune_terminal_actions(state: dict[str, Any]) -> None:
    actions = state.get("pending_actions")
    if not isinstance(actions, list):
        return
    state["pending_actions"] = [
        item
        for item in actions
        if not (isinstance(item, dict) and item.get("status") in {"completed", "failed"})
    ]
