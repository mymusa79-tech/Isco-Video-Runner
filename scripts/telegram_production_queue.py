from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUE_KEY = "production_queue"
RECENT_DISPATCH_SECONDS = 90 * 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_hash(request: dict[str, Any]) -> str:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    encoded = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authorization_id(*, request_id: str, request_sha256: str, requested_at: str, attempt: int, chat_id: int | str) -> str:
    payload = f"{request_id}|{request_sha256}|{requested_at}|{attempt}|{chat_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def validate_ready_request(request: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise RuntimeError("Telegram production request must be an object")
    stored = str(request.get("request_sha256") or "")
    if not stored or stored != _request_hash(request):
        raise RuntimeError("Telegram production request hash is invalid")
    if request.get("approved_by_user") is not True:
        raise RuntimeError("Telegram production request lacks explicit user approval")
    if request.get("source") != "telegram_editorial_control_panel":
        raise RuntimeError("Telegram production request source is invalid")
    if request.get("status") != "approved_waiting_production_activation":
        raise RuntimeError("Telegram production request is not waiting for explicit activation")
    if request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored Telegram request must remain non-dispatching")
    if request.get("kind") not in {"long", "short"}:
        raise RuntimeError("Telegram production request kind is unsupported")
    return request


def release_tag_for(request: dict[str, Any]) -> str:
    validate_ready_request(request)
    request_id = "".join(ch for ch in str(request.get("request_id") or "") if ch.isalnum() or ch in {"-", "_"})
    if not request_id:
        raise RuntimeError("Telegram production request has no safe request id")
    prefix = "short" if request.get("kind") == "short" else "video"
    return f"{prefix}-telegram-{request_id}"[:120]


def _queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.setdefault(QUEUE_KEY, [])
    if not isinstance(value, list):
        raise RuntimeError("Telegram production queue is malformed")
    return value


def latest_ready_request(state: dict[str, Any]) -> dict[str, Any] | None:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        return None
    ready: list[dict[str, Any]] = []
    for request in requests.values():
        if not isinstance(request, dict):
            continue
        try:
            validate_ready_request(request)
        except RuntimeError:
            continue
        ready.append(request)
    if not ready:
        return None
    return max(ready, key=lambda item: (str(item.get("approved_at") or ""), str(item.get("request_id") or "")))


def _age_seconds(timestamp: str) -> float:
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return float("inf")


def enqueue_latest_request(state: dict[str, Any], *, chat_id: int | str) -> tuple[str, dict[str, Any] | None]:
    request = latest_ready_request(state)
    if request is None:
        return "no_ready_request", None
    request_id = str(request["request_id"])
    request_sha256 = str(request["request_sha256"])
    queue = _queue(state)
    matches = [
        item
        for item in queue
        if isinstance(item, dict)
        and item.get("request_id") == request_id
        and item.get("request_sha256") == request_sha256
    ]
    for item in reversed(matches):
        if item.get("status") == "pending_dispatch":
            return "already_queued", item
        if item.get("status") == "dispatch_reserved" and _age_seconds(str(item.get("reserved_at") or "")) < RECENT_DISPATCH_SECONDS:
            return "already_reserved_recent", item
        if item.get("status") == "dispatched" and _age_seconds(str(item.get("dispatched_at") or "")) < RECENT_DISPATCH_SECONDS:
            return "already_dispatched_recent", item

    attempt = 1 + sum(1 for item in matches if item.get("status") in {"dispatch_reserved", "dispatched", "failed"})
    requested_at = _now()
    action = {
        "schema_version": 1,
        "request_id": request_id,
        "request_sha256": request_sha256,
        "release_tag": release_tag_for(request),
        "kind": request.get("kind"),
        "approval_scope": request.get("approval_scope"),
        "chat_id": chat_id,
        "attempt": attempt,
        "status": "pending_dispatch",
        "requested_at": requested_at,
        "authorization_id": _authorization_id(
            request_id=request_id,
            request_sha256=request_sha256,
            requested_at=requested_at,
            attempt=attempt,
            chat_id=chat_id,
        ),
    }
    queue.append(action)
    state["last_event_at"] = requested_at
    return ("retry_queued" if matches else "queued"), action


def pending_dispatch(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = [
        item
        for item in _queue(state)
        if isinstance(item, dict) and item.get("status") == "pending_dispatch"
    ]
    if not pending:
        return None
    return min(pending, key=lambda item: (str(item.get("requested_at") or ""), int(item.get("attempt", 1) or 1)))


def reserve_dispatch(state: dict[str, Any], request_id: str, request_sha256: str) -> dict[str, Any]:
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if item.get("request_id") == request_id and item.get("request_sha256") == request_sha256 and item.get("status") == "pending_dispatch":
            authorization_id = str(item.get("authorization_id") or "").strip()
            if not authorization_id:
                raise RuntimeError("Pending Telegram dispatch has no explicit authorization id")
            item["status"] = "dispatch_reserved"
            item["reserved_at"] = _now()
            state["last_event_at"] = item["reserved_at"]
            return item
    raise RuntimeError("Pending Telegram production dispatch was not found for reservation")


def validate_dispatch_authorization(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
) -> dict[str, Any]:
    authorization_id = str(authorization_id or "").strip()
    if not authorization_id:
        raise RuntimeError("Telegram dispatch authorization id is required")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == authorization_id
            and item.get("status") in {"dispatch_reserved", "dispatched"}
        ):
            return item
    raise RuntimeError("Exact explicit Telegram dispatch authorization was not found")


def mark_dispatched(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    run_url: str = "",
) -> dict[str, Any]:
    authorization_id = str(authorization_id or "").strip()
    if not authorization_id:
        raise RuntimeError("Telegram dispatch authorization id is required")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == authorization_id
            and item.get("status") == "dispatch_reserved"
        ):
            item["status"] = "dispatched"
            item["dispatched_at"] = _now()
            if run_url:
                item["dispatch_run_url"] = run_url
            state["last_event_at"] = item["dispatched_at"]
            return item
    raise RuntimeError("Exact reserved Telegram production dispatch was not found")


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Telegram control state must be an object")
    return data


def _save(path: Path, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _github_output(path: str, **values: object) -> None:
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    reserve = sub.add_parser("reserve")
    reserve.add_argument("--state", required=True, type=Path)
    reserve.add_argument("--request-id", required=True)
    reserve.add_argument("--sha256", required=True)
    reserve.add_argument("--github-output", default="")

    mark = sub.add_parser("mark-dispatched")
    mark.add_argument("--state", required=True, type=Path)
    mark.add_argument("--request-id", required=True)
    mark.add_argument("--sha256", required=True)
    mark.add_argument("--authorization-id", required=True)
    mark.add_argument("--run-url", default="")

    args = parser.parse_args()
    state = _load(args.state)
    if args.command == "reserve":
        item = reserve_dispatch(state, args.request_id, args.sha256)
        _save(args.state, state)
        _github_output(
            args.github_output,
            production_authorization_id=item["authorization_id"],
            production_request_id=item["request_id"],
            production_request_sha256=item["request_sha256"],
        )
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif args.command == "mark-dispatched":
        item = mark_dispatched(
            state,
            args.request_id,
            args.sha256,
            args.authorization_id,
            run_url=args.run_url,
        )
        _save(args.state, state)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
