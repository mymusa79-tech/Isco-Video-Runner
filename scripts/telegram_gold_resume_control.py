from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.telegram_production_queue import validate_ready_request

GOLD_RESUME_QUEUE_KEY = "gold_resume_queue"
LIVE_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved", "dispatch_consumed"})
TERMINAL_STATUSES = frozenset({"completed", "failed"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha40(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 40 or any(ch not in "0123456789abcdef" for ch in text):
        raise RuntimeError(f"{label} must be an exact 40-hex SHA")
    return text


def _sha256(value: object, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise RuntimeError(f"{label} must be an exact SHA-256")
    return text


def _run_id(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text.isdigit() or int(text) < 1:
        raise RuntimeError(f"{label} must be a positive GitHub run integer")
    return text


def _queue(state: dict[str, Any]) -> list[dict[str, Any]]:
    value = state.setdefault(GOLD_RESUME_QUEUE_KEY, [])
    if not isinstance(value, list):
        raise RuntimeError("Telegram Gold resume queue is malformed")
    return value


def _pending_source(state: dict[str, Any], request_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram request registry is missing")
    request = requests.get(str(request_id or ""))
    if not isinstance(request, dict):
        raise RuntimeError("Gold resume request is no longer present")
    validate_ready_request(request)

    production = state.get("production_queue")
    if not isinstance(production, list):
        raise RuntimeError("Telegram production queue is missing")
    entry = next(
        (
            item for item in reversed(production)
            if isinstance(item, dict)
            and str(item.get("request_id") or "") == str(request_id or "")
        ),
        None,
    )
    if not isinstance(entry, dict) or entry.get("status") != "qc_pending":
        raise RuntimeError("This video is not in QC_PENDING")
    if str(entry.get("request_sha256") or "") != str(request.get("request_sha256") or ""):
        raise RuntimeError("QC_PENDING request identity no longer matches approved request")
    pending = entry.get("qc_pending")
    if not isinstance(pending, dict):
        raise RuntimeError("QC_PENDING resume provenance is missing")
    _run_id(pending.get("source_run_id"), "Gold resume source run id")
    _run_id(pending.get("source_run_attempt"), "Gold resume source run attempt")
    _sha40(pending.get("runner_sha"), "Gold resume Runner SHA")
    _sha40(pending.get("engine_sha"), "Gold resume Engine SHA")
    _sha256(pending.get("final_sha256"), "Gold resume final hash")
    artifact = str(pending.get("artifact_name") or "").strip()
    if not artifact.startswith("isco-qc-pending-") or len(artifact) > 160:
        raise RuntimeError("QC_PENDING artifact identity is invalid")
    if str(pending.get("format") or "") not in {"film", "story", "moment"}:
        raise RuntimeError("QC_PENDING format is unsupported")
    return request, entry


def enqueue_gold_resume(
    state: dict[str, Any], request_id: str, *, chat_id: int | str
) -> tuple[str, dict[str, Any] | None]:
    request, source_entry = _pending_source(state, request_id)
    pending = source_entry["qc_pending"]
    source_run_id = str(pending["source_run_id"])
    final_sha = str(pending["final_sha256"])
    queue = _queue(state)

    matches = [
        item for item in queue
        if isinstance(item, dict)
        and item.get("request_id") == request["request_id"]
        and item.get("source_run_id") == source_run_id
        and item.get("final_sha256") == final_sha
    ]
    for item in reversed(matches):
        if item.get("status") in LIVE_STATUSES:
            return "already_requested", item
        if item.get("status") == "completed":
            return "already_completed", item

    requested_at = _now()
    attempt = 1 + sum(1 for item in matches if item.get("status") in TERMINAL_STATUSES)
    auth_payload = (
        f"{request['request_id']}|{request['request_sha256']}|{source_run_id}|"
        f"{final_sha}|{requested_at}|{attempt}|{chat_id}"
    )
    authorization_id = hashlib.sha256(auth_payload.encode("utf-8")).hexdigest()[:32]
    action = {
        "schema_version": 1,
        "request_id": str(request["request_id"]),
        "request_sha256": str(request["request_sha256"]),
        "kind": str(request.get("kind") or ""),
        "approval_scope": str(request.get("approval_scope") or ""),
        "source_run_id": source_run_id,
        "source_run_attempt": str(pending["source_run_attempt"]),
        "artifact_name": str(pending["artifact_name"]),
        "source_runner_sha": str(pending["runner_sha"]),
        "source_engine_sha": str(pending["engine_sha"]),
        "final_sha256": final_sha,
        "format": str(pending["format"]),
        "chat_id": str(chat_id),
        "attempt": attempt,
        "authorization_id": authorization_id,
        "status": "pending_dispatch",
        "requested_at": requested_at,
    }
    queue.append(action)
    state["last_event_at"] = requested_at
    return ("retry_requested" if matches else "requested"), action


def pending_gold_resume(state: dict[str, Any]) -> dict[str, Any] | None:
    for item in _queue(state):
        if isinstance(item, dict) and item.get("status") == "pending_dispatch":
            return item
    return None


def reserve_gold_resume(
    state: dict[str, Any], request_id: str, authorization_id: str, *, runner_sha: str
) -> dict[str, Any]:
    runner_sha = _sha40(runner_sha, "Gold resume dispatch Runner SHA")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == str(request_id or "")
            and item.get("authorization_id") == str(authorization_id or "")
            and item.get("status") == "pending_dispatch"
        ):
            # Revalidate the source right before reserving. A completed/replaced
            # production item can never retain a stale resume authorization.
            _, source = _pending_source(state, request_id)
            pending = source["qc_pending"]
            if (
                str(pending.get("source_run_id")) != str(item.get("source_run_id"))
                or str(pending.get("final_sha256")) != str(item.get("final_sha256"))
                or str(pending.get("artifact_name")) != str(item.get("artifact_name"))
            ):
                raise RuntimeError("Gold resume source identity changed before reservation")
            item["status"] = "dispatch_reserved"
            item["reserved_at"] = _now()
            item["dispatch_runner_sha"] = runner_sha
            state["last_event_at"] = item["reserved_at"]
            return item
    raise RuntimeError("Pending Gold resume authorization was not found")


def validate_gold_resume_authorization(
    state: dict[str, Any], request_id: str, authorization_id: str, *, runner_sha: str
) -> dict[str, Any]:
    runner_sha = _sha40(runner_sha, "Gold resume current Runner SHA")
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if (
            item.get("request_id") == str(request_id or "")
            and item.get("authorization_id") == str(authorization_id or "")
            and item.get("status") == "dispatch_reserved"
        ):
            if str(item.get("dispatch_runner_sha") or "") != runner_sha:
                raise RuntimeError("Gold resume authorization is bound to a different Runner SHA")
            _pending_source(state, request_id)
            return item
    raise RuntimeError("Exact Gold resume authorization was not found")


def consume_gold_resume(
    state: dict[str, Any], request_id: str, authorization_id: str, *, runner_sha: str, workflow_run_id: str
) -> dict[str, Any]:
    item = validate_gold_resume_authorization(state, request_id, authorization_id, runner_sha=runner_sha)
    item["status"] = "dispatch_consumed"
    item["consumed_at"] = _now()
    item["workflow_run_id"] = _run_id(workflow_run_id, "Gold resume workflow run id")
    state["last_event_at"] = item["consumed_at"]
    return item


def mark_gold_resume_completed(state: dict[str, Any], request_id: str, authorization_id: str, *, release_tag: str) -> dict[str, Any]:
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if item.get("request_id") == request_id and item.get("authorization_id") == authorization_id:
            if item.get("status") == "completed":
                return item
            if item.get("status") != "dispatch_consumed":
                raise RuntimeError("Gold resume must be consumed before completion")
            item["status"] = "completed"
            item["completed_at"] = _now()
            item["completed_release_tag"] = str(release_tag or "").strip()
            state["last_event_at"] = item["completed_at"]
            return item
    raise RuntimeError("Gold resume authorization was not found for completion")


def mark_gold_resume_failed(state: dict[str, Any], request_id: str, authorization_id: str, *, reason: str) -> dict[str, Any]:
    for item in _queue(state):
        if not isinstance(item, dict):
            continue
        if item.get("request_id") == request_id and item.get("authorization_id") == authorization_id:
            if item.get("status") == "failed":
                return item
            if item.get("status") not in {"dispatch_reserved", "dispatch_consumed"}:
                raise RuntimeError("Gold resume is not in a fail-able dispatch state")
            item["status"] = "failed"
            item["failed_at"] = _now()
            item["failure_reason"] = str(reason or "gold_resume_failed")[:120]
            state["last_event_at"] = item["failed_at"]
            return item
    raise RuntimeError("Gold resume authorization was not found for failure")


def install(*, active, panel) -> None:
    """Install the Gold-resume command over the already-certified active UI owners."""
    if getattr(panel, "_isco_gold_resume_installed", False):
        return
    prior_handle = panel._handle_command
    # webhook_replay_core invokes active._poll directly after active._install(), while
    # fallback CLI paths invoke panel.poll. Wrap the canonical active owner and bind
    # the same wrapper to both entrypoints so the authorization cannot disappear on
    # webhook ingress.
    prior_poll = active._poll

    def handle(kind, client, state, releases, chat_id):
        if isinstance(kind, str) and kind.startswith("goldresume-"):
            request_id = kind.removeprefix("goldresume-").strip()
            try:
                status, action = enqueue_gold_resume(state, request_id, chat_id=chat_id)
            except RuntimeError:
                client.send(
                    chat_id,
                    "⚠️ لم يعد هذا الفيديو مؤهلًا لـ«تابع Gold». افتح مكتبة الإنتاجات وحدّث حالته.",
                    keyboard=[[{"text": "🎞️ الإنتاجات", "callback_data": "cmd:productions"}]],
                )
                return
            if status == "already_requested":
                text = "⏳ استئناف Gold لهذا الفيديو محجوز أو جارٍ بالفعل. لن أكرر المحاولة."
            elif status == "already_completed":
                text = "✅ Gold لهذا الفيديو اكتمل بالفعل. افتح الفيديو من مكتبة الإنتاجات."
            elif status == "retry_requested":
                text = "▶️ تم اعتماد محاولة Gold جديدة لنفس Final Master بعد انتهاء المحاولة السابقة."
            else:
                text = "▶️ تم اعتماد «تابع Gold» لهذا الفيديو فقط. لن يُعاد التخطيط أو البحث أو الصوت أو الرندر."
            if action is not None:
                text += f"\n\nرقم الطلب: {action.get('request_id')}\nSource Run: {action.get('source_run_id')}"
            client.send(
                chat_id,
                text,
                keyboard=[[{"text": "🎞️ الإنتاجات", "callback_data": "cmd:productions"}]],
            )
            return

        if kind == "produce_latest":
            target = active._current_target(state)
            request_id = str((target or {}).get("request_id") or "")
            if request_id:
                production = state.get("production_queue")
                latest = next(
                    (
                        item for item in reversed(production or [])
                        if isinstance(item, dict) and str(item.get("request_id") or "") == request_id
                    ),
                    None,
                )
                if isinstance(latest, dict) and latest.get("status") == "qc_pending":
                    client.send(
                        chat_id,
                        "🟠 هذا الفيديو وصل بالفعل إلى Final Master وينتظر Gold. لن أعيد Production كاملًا. افتحه من «🎞️ الإنتاجات» واضغط «▶️ تابع Gold».",
                        keyboard=[[{"text": "🎞️ الإنتاجات", "callback_data": "cmd:productions"}]],
                    )
                    return
        return prior_handle(kind, client, state, releases, chat_id)

    def poll(state_path: Path) -> None:
        prior_poll(state_path)
        state = panel.load_state(state_path)
        action = pending_gold_resume(state)
        panel._github_output("needs_gold_resume", "true" if action is not None else "false")
        for key, field in (
            ("gold_resume_request_id", "request_id"),
            ("gold_resume_authorization_id", "authorization_id"),
            ("gold_resume_source_run_id", "source_run_id"),
            ("gold_resume_artifact_name", "artifact_name"),
        ):
            panel._github_output(key, str(action.get(field) or "") if action else "")

    panel._handle_command = handle
    active._poll = poll
    panel.poll = poll
    panel._isco_gold_resume_installed = True
