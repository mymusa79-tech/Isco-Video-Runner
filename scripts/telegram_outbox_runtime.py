from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from scripts.orchestration_telegram_ingress_outbox import (
    OutboxLedger,
    OutboxMessage,
    OutboxStatus,
    TelegramControlContractError,
    payload_sha256,
)
from scripts.telegram_control_panel import load_state, save_state

STATE_KEY = "telegram_outbox_v1"
SCHEMA_VERSION = 1


def _read_secret_file_required(env_name: str) -> str:
    path = str(os.environ.get(env_name) or "").strip()
    if not path:
        raise TelegramControlContractError(f"{env_name} is not set")
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise TelegramControlContractError(f"{env_name} is empty")
    return value


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _canonical_request(request: Mapping[str, Any], chat_id: str) -> dict[str, Any]:
    if int(request.get("schema_version") or 0) != SCHEMA_VERSION:
        raise TelegramControlContractError("unsupported Telegram outbox request schema")
    method = str(request.get("method") or "").strip()
    if method != "sendMessage":
        raise TelegramControlContractError("L6 outbox runtime currently permits sendMessage only")
    payload = request.get("payload")
    if not isinstance(payload, Mapping):
        raise TelegramControlContractError("Telegram outbox request payload must be an object")
    text = str(payload.get("text") or "").strip()
    if not text:
        raise TelegramControlContractError("Telegram sendMessage request requires text")
    normalized_payload = dict(payload)
    normalized_payload["chat_id"] = chat_id
    return {
        "schema_version": SCHEMA_VERSION,
        "method": method,
        "payload": normalized_payload,
    }


def _message_to_dict(message: OutboxMessage) -> dict[str, Any]:
    return {
        "outbox_message_id": message.outbox_message_id,
        "bot_token_hash": message.bot_token_hash,
        "chat_id": message.chat_id,
        "message_kind": message.message_kind,
        "correlation_id": message.correlation_id,
        "payload_hash": message.payload_hash,
        "journal_event_ref": message.journal_event_ref,
        "status": message.status.value,
        "attempts": message.attempts,
        "created_at": message.created_at,
        "next_retry_at": message.next_retry_at,
        "telegram_message_id": message.telegram_message_id,
    }


def _message_from_dict(raw: Mapping[str, Any]) -> OutboxMessage:
    return OutboxMessage(
        outbox_message_id=str(raw.get("outbox_message_id") or ""),
        bot_token_hash=str(raw.get("bot_token_hash") or ""),
        chat_id=str(raw.get("chat_id") or ""),
        message_kind=str(raw.get("message_kind") or ""),
        correlation_id=str(raw.get("correlation_id") or ""),
        payload_hash=str(raw.get("payload_hash") or ""),
        journal_event_ref=str(raw.get("journal_event_ref") or ""),
        status=OutboxStatus(str(raw.get("status") or "")),
        attempts=int(raw.get("attempts") or 0),
        created_at=str(raw.get("created_at") or ""),
        next_retry_at=(str(raw.get("next_retry_at")) if raw.get("next_retry_at") else None),
        telegram_message_id=(str(raw.get("telegram_message_id")) if raw.get("telegram_message_id") else None),
    )


def _restore_ledger(message: OutboxMessage) -> OutboxLedger:
    """Hydrate one durable snapshot without pretending it is a new enqueue.

    `OutboxLedger.enqueue()` intentionally accepts only a brand-new PENDING message
    with zero attempts. A durable snapshot may legitimately be SENDING, SENT,
    FAILED, RECONCILIATION_REQUIRED, or a retried PENDING message. This persistence
    adapter validates those snapshot invariants once, then loads the immutable
    record into the same ledger whose public methods continue to own all state
    transitions.
    """
    if message.status is not OutboxStatus.PENDING and message.attempts < 1:
        raise TelegramControlContractError("persisted non-PENDING outbox message requires at least one attempt")
    if message.status is not OutboxStatus.SENT and message.telegram_message_id is not None:
        raise TelegramControlContractError("only persisted SENT outbox message may carry telegram_message_id")
    if message.status in {OutboxStatus.SENDING, OutboxStatus.RECONCILIATION_REQUIRED, OutboxStatus.SENT} and message.next_retry_at is not None:
        raise TelegramControlContractError("active/ambiguous/sent outbox snapshot cannot carry next_retry_at")
    ledger = OutboxLedger()
    # Persistence hydration is intentionally distinct from enqueue semantics. The
    # ledger remains the sole owner of every transition after this one load seam.
    ledger._messages[message.outbox_message_id] = message
    return ledger


def _entries(state: dict[str, Any]) -> dict[str, Any]:
    raw = state.setdefault(STATE_KEY, {})
    if not isinstance(raw, dict):
        raise TelegramControlContractError("Telegram outbox state is malformed")
    return raw


def _load_request(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TelegramControlContractError("Telegram outbox request must be an object")
    return data


def _load_record(state_path: Path, outbox_message_id: str) -> tuple[dict[str, Any], dict[str, Any], OutboxMessage]:
    state = load_state(Path(state_path))
    entries = _entries(state)
    record = entries.get(outbox_message_id)
    if not isinstance(record, dict) or not isinstance(record.get("message"), dict):
        raise TelegramControlContractError("unknown outbox_message_id")
    return state, record, _message_from_dict(record["message"])


def enqueue(state_path: Path, request_path: Path) -> OutboxMessage:
    token = _read_secret_file_required("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = _read_secret_file_required("TELEGRAM_CHAT_ID_FILE")
    request = _load_request(request_path)
    canonical = _canonical_request(request, chat_id)
    outbox_message_id = str(request.get("outbox_message_id") or "").strip()
    message_kind = str(request.get("message_kind") or "").strip()
    correlation_id = str(request.get("correlation_id") or "").strip()
    journal_event_ref = str(request.get("journal_event_ref") or "").strip()
    created_at = str(request.get("created_at") or "").strip()
    message = OutboxMessage.pending(
        outbox_message_id=outbox_message_id,
        bot_token_hash=_token_hash(token),
        chat_id=chat_id,
        message_kind=message_kind,
        correlation_id=correlation_id,
        payload_hash=payload_sha256(canonical),
        journal_event_ref=journal_event_ref,
        created_at=created_at,
    )
    state = load_state(Path(state_path))
    entries = _entries(state)
    existing = entries.get(outbox_message_id)
    if existing is not None:
        if not isinstance(existing, dict) or not isinstance(existing.get("request"), dict):
            raise TelegramControlContractError("existing Telegram outbox record is malformed")
        current = _message_from_dict(existing["message"])
        ledger = _restore_ledger(current)
        ledger.enqueue(message)
        if existing["request"] != canonical:
            raise TelegramControlContractError("outbox_message_id reused with conflicting request payload")
        return current
    entries[outbox_message_id] = {"message": _message_to_dict(message), "request": canonical}
    save_state(Path(state_path), state)
    return message


def begin_send(state_path: Path, outbox_message_id: str) -> tuple[OutboxMessage, bool]:
    state, record, current = _load_record(state_path, outbox_message_id)
    ledger = _restore_ledger(current)
    if current.status is OutboxStatus.SENDING:
        recovered = ledger.recover_interrupted_send(outbox_message_id)
        record["message"] = _message_to_dict(recovered)
        save_state(Path(state_path), state)
        return recovered, False
    if current.status in {OutboxStatus.SENT, OutboxStatus.RECONCILIATION_REQUIRED}:
        return current, False
    sending = ledger.begin_send(outbox_message_id)
    record["message"] = _message_to_dict(sending)
    save_state(Path(state_path), state)
    return sending, True


def send_current(state_path: Path, outbox_message_id: str) -> OutboxMessage:
    token = _read_secret_file_required("TELEGRAM_BOT_TOKEN_FILE")
    state, record, current = _load_record(state_path, outbox_message_id)
    if not isinstance(record.get("request"), dict):
        raise TelegramControlContractError("outbox request state is malformed")
    if current.status is not OutboxStatus.SENDING:
        raise TelegramControlContractError("outbox send requires durable SENDING state")
    request = record["request"]
    url = f"https://api.telegram.org/bot{token}/{request['method']}"
    response = requests.post(url, json=request["payload"], timeout=35)
    response.raise_for_status()
    body = response.json()
    if not body.get("ok") or not isinstance(body.get("result"), Mapping):
        raise RuntimeError(f"Telegram {request['method']} failed")
    message_id = str(body["result"].get("message_id") or "").strip()
    if not message_id:
        raise RuntimeError("Telegram send returned no message_id")
    ledger = _restore_ledger(current)
    sent = ledger.mark_sent(outbox_message_id, telegram_message_id=message_id)
    record["message"] = _message_to_dict(sent)
    save_state(Path(state_path), state)
    return sent


def reconcile_sent(state_path: Path, outbox_message_id: str, telegram_message_id: str) -> OutboxMessage:
    """Resolve an ambiguous send only from explicit provider evidence.

    This path intentionally has no Telegram token and performs no provider call. An
    operator/reconciler must supply the concrete Telegram message id recovered from
    independent evidence; otherwise the ambiguous send remains blocked.
    """
    message_id = str(telegram_message_id or "").strip()
    if not message_id:
        raise TelegramControlContractError("reconcile-sent requires telegram_message_id evidence")
    state, record, current = _load_record(state_path, outbox_message_id)
    ledger = _restore_ledger(current)
    reconciled = ledger.reconcile(outbox_message_id, confirmed_sent_message_id=message_id)
    record["message"] = _message_to_dict(reconciled)
    save_state(Path(state_path), state)
    return reconciled


def reconcile_absent(state_path: Path, outbox_message_id: str, *, next_retry_at: str | None = None) -> OutboxMessage:
    """Return an ambiguous send to PENDING only after absence is externally proven.

    `confirmed_absent=True` is represented by choosing this explicit command. No
    Telegram credential is accepted or used here, so reconciliation itself can never
    become a hidden second outbound owner or blind resend path.
    """
    state, record, current = _load_record(state_path, outbox_message_id)
    ledger = _restore_ledger(current)
    reconciled = ledger.reconcile(
        outbox_message_id,
        confirmed_absent=True,
        next_retry_at=(str(next_retry_at).strip() if next_retry_at else None),
    )
    record["message"] = _message_to_dict(reconciled)
    save_state(Path(state_path), state)
    return reconciled


def _github_output(name: str, value: str) -> None:
    path = str(os.environ.get("GITHUB_OUTPUT") or "").strip()
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_enqueue = sub.add_parser("enqueue")
    p_enqueue.add_argument("--state", required=True, type=Path)
    p_enqueue.add_argument("--request", required=True, type=Path)
    p_begin = sub.add_parser("begin")
    p_begin.add_argument("--state", required=True, type=Path)
    p_begin.add_argument("--outbox-message-id", required=True)
    p_send = sub.add_parser("send")
    p_send.add_argument("--state", required=True, type=Path)
    p_send.add_argument("--outbox-message-id", required=True)
    p_reconcile_sent = sub.add_parser("reconcile-sent")
    p_reconcile_sent.add_argument("--state", required=True, type=Path)
    p_reconcile_sent.add_argument("--outbox-message-id", required=True)
    p_reconcile_sent.add_argument("--telegram-message-id", required=True)
    p_reconcile_absent = sub.add_parser("reconcile-absent")
    p_reconcile_absent.add_argument("--state", required=True, type=Path)
    p_reconcile_absent.add_argument("--outbox-message-id", required=True)
    p_reconcile_absent.add_argument("--next-retry-at")
    args = parser.parse_args()

    if args.command == "enqueue":
        message = enqueue(args.state, args.request)
        _github_output("outbox_message_id", message.outbox_message_id)
        _github_output("outbox_status", message.status.value)
        return
    if args.command == "begin":
        message, allowed = begin_send(args.state, args.outbox_message_id)
        _github_output("send_allowed", "true" if allowed else "false")
        _github_output("outbox_status", message.status.value)
        return
    if args.command == "send":
        message = send_current(args.state, args.outbox_message_id)
    elif args.command == "reconcile-sent":
        message = reconcile_sent(args.state, args.outbox_message_id, args.telegram_message_id)
    else:
        message = reconcile_absent(args.state, args.outbox_message_id, next_retry_at=args.next_retry_at)
    _github_output("outbox_status", message.status.value)
    _github_output("telegram_message_id", str(message.telegram_message_id or ""))


if __name__ == "__main__":
    main()
