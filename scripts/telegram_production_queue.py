from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUE_KEY = "production_queue"
RECENT_DISPATCH_SECONDS = 90 * 60
# Canonical V4 has a 120-minute job timeout. Keep consumed handoffs visible a little
# beyond that bound so an active production never disappears from status solely due
# to clock drift, while still preventing orphaned ledger history from looking live
# forever after infrastructure-level terminal reconciliation loss.
CONSUMED_ACTIVE_SECONDS = 125 * 60
LIVE_DISPATCH_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved"})
ACTIVE_PRODUCTION_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved", "dispatch_consumed"})
FAILABLE_DISPATCH_STATUSES = frozenset({"dispatch_reserved", "dispatch_consumed"})
DISPATCH_FAILURE_REASONS = frozenset({"workflow_dispatch_failed", "production_failed", "production_cancelled"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_hash(request: dict[str, Any]) -> str:
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    encoded = json.dumps(subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authorization_id(*, request_id: str, request_sha256: str, requested_at: str, attempt: int, chat_id: int | str) -> str:
    payload = f"{request_id}|{request_sha256}|{requested_at}|{attempt}|{chat_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def _runner_sha(value: str) -> str:
    value = str(value or "").strip().lower()
    if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
        raise RuntimeError("Telegram dispatch reservation requires an exact 40-hex Runner SHA")
    return value


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


def _age_seconds(timestamp: str) -> float:
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return float("inf")


def dispatch_entry_is_live(item: dict[str, Any]) -> bool:
    """Project durable ledger history into a bounded live orchestration lease.

    The ledger intentionally keeps old attempts for replay/idempotency evidence. A
    pre-handoff request/reservation is live only during the same 90-minute window
    used by retry protection. A consumed authorization may remain live through the
    canonical V4 120-minute timeout (+5 minutes clock/teardown margin). Terminal or
    malformed entries are never live.
    """
    if not isinstance(item, dict):
        return False
    status = str(item.get("status") or "")
    if status == "pending_dispatch":
        return _age_seconds(str(item.get("requested_at") or "")) < RECENT_DISPATCH_SECONDS
    if status == "dispatch_reserved":
        return _age_seconds(str(item.get("reserved_at") or "")) < RECENT_DISPATCH_SECONDS
    if status == "dispatch_consumed":
        return _age_seconds(str(item.get("consumed_at") or "")) < CONSUMED_ACTIVE_SECONDS
    return False


def live_dispatches(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return live pre-V4 handoffs only (pending + reserved), never ledger history."""
    return [
        item
        for item in _queue(state)
        if isinstance(item, dict)
        and item.get("status") in LIVE_DISPATCH_STATUSES
        and dispatch_entry_is_live(item)
    ]


def live_dispatch_count(state: dict[str, Any]) -> int:
    return len(live_dispatches(state))


def live_production_dispatches(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bounded live pending/reserved/consumed entries for operator status."""
    return [
        item
        for item in _queue(state)
        if isinstance(item, dict)
        and item.get("status") in ACTIVE_PRODUCTION_STATUSES
        and dispatch_entry_is_live(item)
    ]


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


def ready_request_by_id(state: dict[str, Any], request_id: str, request_sha256: str) -> dict[str, Any] | None:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        return None
    request = requests.get(str(request_id or ""))
    if not isinstance(request, dict):
        return None
    try:
        validate_ready_request(request)
    except RuntimeError:
        return None
    if str(request.get("request_sha256") or "") != str(request_sha256 or ""):
        return None
    return request


def _enqueue_ready_request(
    state: dict[str, Any], request: dict[str, Any], *, chat_id: int | str
) -> tuple[str, dict[str, Any] | None]:
    validate_ready_request(request)
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
        if item.get("status") == "completed":
            return "already_completed", item
        if item.get("status") == "pending_dispatch" and dispatch_entry_is_live(item):
            return "already_queued", item
        if item.get("status") == "dispatch_reserved" and _age_seconds(str(item.get("reserved_at") or "")) < RECENT_DISPATCH_SECONDS:
            return "already_reserved_recent", item
        if item.get("status") == "dispatch_consumed" and _age_seconds(str(item.get("consumed_at") or "")) < RECENT_DISPATCH_SECONDS:
            return "already_dispatched_recent", item

    attempt = 1 + sum(
        1
        for item in matches
        if item.get("status") in {"dispatch_reserved", "dispatch_consumed", "completed", "failed"}
    )
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


def enqueue_request(
    state: dict[str, Any], request_id: str, request_sha256: str, *, chat_id: int | str
) -> tuple[str, dict[str, Any] | None]:
    request = ready_request_by_id(state, request_id, request_sha256)
    if request is None:
        return "no_ready_request", None
    return _enqueue_ready_request(state, request, chat_id=chat_id)


def enqueue_latest_request(state: dict[str, Any], *, chat_id: int | str) -> tuple[str, dict[str, Any] | None]:
    request = latest_ready_request(state)
    if request is None:
        return "no_ready_request", None
    return _enqueue_ready_request(state, request, chat_id=chat_id)


def pending_dispatch(state: dict[str, Any]) -> dict[str, Any] | None:
    pending = [
        item
        for item in _queue(state)
        if isinstance(item, dict)
        and item.get("status") == "pending_dispatch"
        and dispatch_entry_is_live(item)
    ]
    if not pending:
        return None
    return min(pending, key=lambda item: (str(item.get("requested_at") or ""), int(item.get("attempt", 1) or 1)))


def reserve_dispatch(
    state: dict[str, Any], request_id: str, request_sha256: str, *, runner_sha: str
) -> dict[str, Any]:
    bound_runner_sha = _runner_sha(runner_sha)
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if item.get("request_id") == request_id and item.get("request_sha256") == request_sha256 and item.get("status") == "pending_dispatch":
            authorization_id = str(item.get("authorization_id") or "").strip()
            if not authorization_id:
                raise RuntimeError("Pending Telegram dispatch has no explicit authorization id")
            item["status"] = "dispatch_reserved"
            item["reserved_at"] = _now()
            item["runner_sha"] = bound_runner_sha
            state["last_event_at"] = item["reserved_at"]
            return item
    raise RuntimeError("Pending Telegram production dispatch was not found for reservation")


def validate_dispatch_authorization(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    runner_sha: str = "",
) -> dict[str, Any]:
    authorization_id = str(authorization_id or "").strip()
    if not authorization_id:
        raise RuntimeError("Telegram dispatch authorization id is required")
    expected_runner_sha = _runner_sha(runner_sha) if str(runner_sha or "").strip() else ""
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == authorization_id
            and item.get("status") == "dispatch_reserved"
        ):
            bound = str(item.get("runner_sha") or "").strip().lower()
            if expected_runner_sha and bound != expected_runner_sha:
                raise RuntimeError("Telegram dispatch authorization is bound to a different Runner SHA")
            if expected_runner_sha and not bound:
                raise RuntimeError("Telegram dispatch authorization lacks Runner SHA binding")
            return item
    raise RuntimeError("Exact explicit Telegram dispatch authorization was not found")


def consume_dispatch_authorization(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    workflow_run_id: str = "",
    runner_sha: str = "",
) -> dict[str, Any]:
    item = validate_dispatch_authorization(
        state,
        request_id,
        request_sha256,
        authorization_id,
        runner_sha=runner_sha,
    )
    item["status"] = "dispatch_consumed"
    item["consumed_at"] = _now()
    if workflow_run_id:
        item["workflow_run_id"] = str(workflow_run_id)
    state["last_event_at"] = item["consumed_at"]
    return item


def mark_dispatch_completed(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    release_tag: str,
) -> dict[str, Any]:
    release_tag = str(release_tag or "").strip()
    if not release_tag:
        raise RuntimeError("Completed Telegram dispatch requires a release tag")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == str(authorization_id or "").strip()
        ):
            if item.get("status") == "completed":
                if item.get("completed_release_tag") != release_tag:
                    raise RuntimeError("Completed Telegram dispatch release identity changed")
                return item
            if item.get("status") != "dispatch_consumed":
                raise RuntimeError("Telegram dispatch is not consumed and cannot become completed")
            completed_at = _now()
            item["status"] = "completed"
            item["completed_at"] = completed_at
            item["completed_release_tag"] = release_tag
            state["last_event_at"] = completed_at
            return item
    raise RuntimeError("Exact Telegram dispatch authorization was not found for completion transition")


def mark_dispatch_failed(
    state: dict[str, Any],
    request_id: str,
    request_sha256: str,
    authorization_id: str,
    *,
    reason: str,
) -> dict[str, Any]:
    reason = str(reason or "").strip()
    if reason not in DISPATCH_FAILURE_REASONS:
        raise RuntimeError("Unsupported Telegram dispatch failure reason")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == request_id
            and item.get("request_sha256") == request_sha256
            and item.get("authorization_id") == str(authorization_id or "").strip()
        ):
            if item.get("status") == "failed":
                return item
            if item.get("status") == "completed":
                raise RuntimeError("Completed Telegram dispatch cannot transition back to failed")
            if item.get("status") not in FAILABLE_DISPATCH_STATUSES:
                raise RuntimeError("Telegram dispatch is not in a fail-able state")
            failed_at = _now()
            item["status"] = "failed"
            item["failed_at"] = failed_at
            item["failure_reason"] = reason
            state["last_event_at"] = failed_at
            return item
    raise RuntimeError("Exact Telegram dispatch authorization was not found for failure transition")


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
    reserve.add_argument("--runner-sha", default="")
    reserve.add_argument("--github-output", default="")

    consume = sub.add_parser("consume")
    consume.add_argument("--state", required=True, type=Path)
    consume.add_argument("--request-id", required=True)
    consume.add_argument("--sha256", required=True)
    consume.add_argument("--authorization-id", required=True)
    consume.add_argument("--workflow-run-id", default="")
    consume.add_argument("--runner-sha", default="")

    complete = sub.add_parser("complete")
    complete.add_argument("--state", required=True, type=Path)
    complete.add_argument("--request-id", required=True)
    complete.add_argument("--sha256", required=True)
    complete.add_argument("--authorization-id", required=True)
    complete.add_argument("--release-tag", required=True)

    fail = sub.add_parser("fail")
    fail.add_argument("--state", required=True, type=Path)
    fail.add_argument("--request-id", required=True)
    fail.add_argument("--sha256", required=True)
    fail.add_argument("--authorization-id", required=True)
    fail.add_argument("--reason", required=True, choices=sorted(DISPATCH_FAILURE_REASONS))

    args = parser.parse_args()
    state = _load(args.state)
    if args.command == "reserve":
        runner_sha = str(args.runner_sha or os.environ.get("GITHUB_SHA") or "").strip()
        item = reserve_dispatch(state, args.request_id, args.sha256, runner_sha=runner_sha)
        _save(args.state, state)
        _github_output(
            args.github_output,
            production_authorization_id=item["authorization_id"],
            production_request_id=item["request_id"],
            production_request_sha256=item["request_sha256"],
            production_runner_sha=item["runner_sha"],
        )
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif args.command == "consume":
        item = consume_dispatch_authorization(
            state,
            args.request_id,
            args.sha256,
            args.authorization_id,
            workflow_run_id=args.workflow_run_id,
            runner_sha=args.runner_sha,
        )
        _save(args.state, state)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif args.command == "complete":
        item = mark_dispatch_completed(
            state,
            args.request_id,
            args.sha256,
            args.authorization_id,
            release_tag=args.release_tag,
        )
        _save(args.state, state)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif args.command == "fail":
        item = mark_dispatch_failed(
            state,
            args.request_id,
            args.sha256,
            args.authorization_id,
            reason=args.reason,
        )
        _save(args.state, state)
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
