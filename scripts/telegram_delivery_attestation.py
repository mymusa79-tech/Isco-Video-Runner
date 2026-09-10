from __future__ import annotations

from typing import Any


class TelegramTargetAttestationError(RuntimeError):
    """Telegram accepted a request but did not prove the intended destination."""


def _numeric_id(value: object) -> int:
    text = str(value or "").strip()
    if not text or text.startswith("+"):
        raise TelegramTargetAttestationError("Telegram target identity is not canonical")
    try:
        return int(text, 10)
    except (TypeError, ValueError) as exc:
        raise TelegramTargetAttestationError("Telegram target identity is not numeric") from exc


def attest_configured_target(*, chat_id: object, allowed_user_id: object = "") -> int:
    """Return canonical target id and independently bind private chats to the operator.

    Positive Telegram chat ids are private-user chats, so the separately configured
    allowed operator id is mandatory and must identify that same user. Negative ids are
    group/supergroup destinations and cannot be equated with a user id; those remain
    bound by the returned Bot API chat identity instead of a false user/group equality.
    """
    target = _numeric_id(chat_id)
    allowed = str(allowed_user_id or "").strip()
    if target > 0:
        if not allowed:
            raise TelegramTargetAttestationError(
                "Telegram private target cannot be independently attested"
            )
        if _numeric_id(allowed) != target:
            raise TelegramTargetAttestationError(
                "Telegram private target does not match the authorized operator"
            )
    return target


def attest_message_response(
    body: object,
    *,
    expected_chat_id: object,
    expected_message_id: object | None = None,
) -> int:
    """Validate Bot API message identity before callers persist or continue editing it."""
    expected_chat = _numeric_id(expected_chat_id)
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise TelegramTargetAttestationError("Telegram response is not a successful envelope")
    result: Any = body.get("result")
    if not isinstance(result, dict):
        raise TelegramTargetAttestationError("Telegram response is missing message result")
    chat = result.get("chat")
    if not isinstance(chat, dict):
        raise TelegramTargetAttestationError("Telegram response is missing target identity")
    actual_chat = _numeric_id(chat.get("id"))
    if actual_chat != expected_chat:
        raise TelegramTargetAttestationError("Telegram response target does not match configured target")
    message_id = result.get("message_id")
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        raise TelegramTargetAttestationError("Telegram response is missing canonical message id")
    if expected_message_id is not None:
        try:
            expected_message = int(str(expected_message_id).strip(), 10)
        except (TypeError, ValueError) as exc:
            raise TelegramTargetAttestationError("Telegram expected message identity is invalid") from exc
        if message_id != expected_message:
            raise TelegramTargetAttestationError("Telegram edited message identity changed unexpectedly")
    return message_id
