from __future__ import annotations

from typing import Any

from scripts import telegram_bot_api_10_3_ui as bot_api_10_3
from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_production_rich_ui as rich


_INSTALLED = False
_ACTIVE_DISPATCH_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved", "dispatch_consumed"})


def _release_to_delivery(releases: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    if hasattr(releases, "latest"):
        for prefix in ("video-", "short-"):
            try:
                item = releases.latest(prefix)
            except Exception:
                item = None
            if isinstance(item, dict):
                candidates.append(item)
        if not candidates:
            try:
                item = releases.latest()
            except Exception:
                item = None
            if isinstance(item, dict):
                candidates.append(item)
    elif isinstance(releases, list):
        candidates = [item for item in releases if isinstance(item, dict)]
    if not candidates:
        return {}
    return max(
        candidates,
        key=lambda item: (
            str(item.get("published_at") or item.get("created_at") or item.get("updated_at") or ""),
            str(item.get("tag_name") or ""),
        ),
    )


def _queue_actions(state: dict[str, Any]) -> list[dict[str, Any]]:
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        return []
    return [item for item in queue if isinstance(item, dict) and str(item.get("request_id") or "").strip()]


def _latest_queue_action(state: dict[str, Any]) -> dict[str, Any] | None:
    items = _queue_actions(state)
    if not items:
        return None
    return max(
        items,
        key=lambda item: (
            str(item.get("completed_at") or item.get("failed_at") or item.get("consumed_at") or item.get("reserved_at") or item.get("requested_at") or ""),
            int(item.get("attempt", 0) or 0),
        ),
    )


def _latest_active_queue_action(state: dict[str, Any]) -> dict[str, Any] | None:
    items = [item for item in _queue_actions(state) if str(item.get("status") or "") in _ACTIVE_DISPATCH_STATUSES]
    if not items:
        return None
    return max(
        items,
        key=lambda item: (
            str(item.get("consumed_at") or item.get("reserved_at") or item.get("requested_at") or ""),
            int(item.get("attempt", 0) or 0),
        ),
    )


def _request_for(state: dict[str, Any], request_id: str) -> dict[str, Any] | None:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        return None
    value = requests.get(request_id)
    return value if isinstance(value, dict) else None


def _workflow_run(releases: Any, run_id: str) -> dict[str, Any] | None:
    repository = str(getattr(releases, "repository", "") or "").strip()
    if not repository or not str(run_id or "").isdigit() or not hasattr(releases, "_get"):
        return None
    try:
        run = releases._get(f"https://api.github.com/repos/{repository}/actions/runs/{run_id}")
    except Exception:
        return None
    return run if isinstance(run, dict) else None


def _workflow_jobs(releases: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    jobs_url = str(run.get("jobs_url") or "").strip()
    if not jobs_url.startswith("https://api.github.com/") or not hasattr(releases, "_get"):
        return []
    try:
        data = releases._get(jobs_url)
    except Exception:
        return []
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return [item for item in jobs if isinstance(item, dict)] if isinstance(jobs, list) else []


def _step_stage(step_name: str) -> str:
    value = step_name.casefold()
    if "approval" in value or "authorization" in value or "idempotency" in value:
        return "التحقق من التفويض"
    if "checkout" in value or "install" in value or "provider authentication" in value or "voice fallback" in value:
        return "تهيئة الإنتاج"
    if "produce with canonical v4 runtime" in value or "run exact approved telegram production" in value:
        return "الإنتاج: التخطيط → الكتابة → الصوت → المونتاج"
    if "quality" in value or "master qc" in value or "verify deterministic" in value or "validate" in value:
        return "فحص الجودة"
    if "release" in value or "delivery" in value:
        return "الحزمة النهائية"
    return step_name or "الإنتاج الجاري"


def _run_progress(run: dict[str, Any], jobs: list[dict[str, Any]]) -> tuple[str, int | None, str]:
    conclusion = str(run.get("conclusion") or "").strip()
    status = str(run.get("status") or "").strip()
    if conclusion == "success":
        return "completed", 100, "اكتمل Workflow بنجاح."
    if conclusion in {"failure", "timed_out"}:
        for job in jobs:
            for step in job.get("steps") or []:
                if isinstance(step, dict) and str(step.get("conclusion") or "") in {"failure", "timed_out"}:
                    name = str(step.get("name") or "").strip()
                    return "failed", None, f"توقف عند: {name}" if name else "فشل Workflow."
        return "failed", None, "فشل Workflow."
    if conclusion == "cancelled":
        return "cancelled", None, "تم إلغاء Workflow."

    steps = [step for job in jobs for step in (job.get("steps") or []) if isinstance(step, dict)]
    current = next((step for step in steps if str(step.get("status") or "") == "in_progress"), None)
    completed = sum(1 for step in steps if str(step.get("status") or "") == "completed")
    progress = int(round(completed * 100 / len(steps))) if steps else None
    if current:
        name = str(current.get("name") or "").strip()
        return _step_stage(name), progress, f"الخطوة الحالية في GitHub Actions: {name}"
    return status or "in_progress", progress, "Workflow قيد التنفيذ."


def _status_payload(state: dict[str, Any], releases: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in ("stage", "phase", "status", "progress", "message", "detail", "note", "title", "topic", "approved_topic", "run_id", "request_id"):
        if key in state:
            payload[key] = state.get(key)

    # Status is a live projection, not a history viewer. A completed/failed ledger
    # entry must never hide a newly approved target that still awaits user action.
    action = _latest_active_queue_action(state)
    if action:
        request_id = str(action.get("request_id") or "").strip()
        payload["request_id"] = request_id
        request = _request_for(state, request_id)
        if request:
            payload["title"] = request.get("approved_topic") or payload.get("title")
        action_status = str(action.get("status") or "").strip()
        payload["stage"] = action_status or payload.get("stage")
        run_id = str(action.get("workflow_run_id") or "").strip()
        if run_id:
            payload["run_id"] = run_id
            run = _workflow_run(releases, run_id)
            if run:
                stage, progress, note = _run_progress(run, _workflow_jobs(releases, run))
                payload["stage"] = stage
                if progress is not None:
                    payload["progress"] = progress
                payload["note"] = note
                number = str(run.get("run_number") or "").strip()
                if number:
                    payload["run_id"] = f"Run #{number}"
        elif action_status == "pending_dispatch":
            payload["note"] = "تم اعتماد التشغيل ويجري تسليمه فورًا؛ لا يوجد طابور انتظار للمستخدم."
        elif action_status == "dispatch_reserved":
            payload["note"] = "حُجز التفويض لمرة واحدة ويجري تسليمه إلى V4 الموحد."
    else:
        target = state.get(getattr(active, "PRODUCTION_TARGET_KEY", "production_target"))
        if isinstance(target, dict):
            request_id = str(target.get("request_id") or "").strip()
            payload["request_id"] = request_id
            request = _request_for(state, request_id)
            if request:
                payload["title"] = request.get("approved_topic") or payload.get("title")
                payload["stage"] = request.get("status") or "approved_waiting_production_activation"
                payload["note"] = "الموضوع معتمد، لكن Production لم يبدأ؛ حاجز «تأكيد الإنتاج» ما زال مطلوبًا."

    if not payload.get("stage"):
        latest = _release_to_delivery(releases)
        if latest:
            payload["stage"] = "completed"
            payload["progress"] = 100
            payload["run_id"] = latest.get("tag_name")
            payload["note"] = "لا يوجد تشغيل نشط؛ هذه آخر حزمة مكتملة."
        else:
            payload["stage"] = "غير نشط"
            payload["note"] = "لا يوجد Production Run نشط أو تسليم سابق."
    return payload


def _flatten_quality(value: Any, prefix: str = "", depth: int = 0) -> list[dict[str, Any]]:
    if depth > 3:
        return []
    rows: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, bool):
                rows.append({"name": name, "passed": item})
            elif isinstance(item, str) and item.casefold() in {"pass", "passed", "success", "fail", "failed", "failure", "warn", "warning"}:
                rows.append({"name": name, "status": item})
            elif isinstance(item, dict):
                rows.extend(_flatten_quality(item, name, depth + 1))
    return rows


def _failed_run_report(releases: Any, action: dict[str, Any] | None) -> dict[str, Any] | None:
    if not action:
        return None
    run_id = str(action.get("workflow_run_id") or "").strip()
    run = _workflow_run(releases, run_id)
    if not run or str(run.get("conclusion") or "") not in {"failure", "timed_out", "cancelled"}:
        return None
    jobs = _workflow_jobs(releases, run)
    failed_job = ""
    failed_step = ""
    for job in jobs:
        if str(job.get("conclusion") or "") not in {"failure", "timed_out", "cancelled"}:
            continue
        failed_job = str(job.get("name") or "").strip()
        for step in job.get("steps") or []:
            if isinstance(step, dict) and str(step.get("conclusion") or "") in {"failure", "timed_out", "cancelled"}:
                failed_step = str(step.get("name") or "").strip()
                break
        break
    label = failed_step or failed_job or "GitHub Actions"
    return {
        "run_id": f"Run #{run.get('run_number')}" if run.get("run_number") else run_id,
        "gates": [{"name": label, "status": "fail", "detail": f"Job: {failed_job}" if failed_job and failed_step else ""}],
        "error": f"Workflow انتهى بالحالة: {run.get('conclusion')}",
    }


def _quality_payload(state: dict[str, Any], releases: Any) -> dict[str, Any] | None:
    for key in ("quality_report", "quality_gates", "last_quality_report", "failure_report"):
        value = state.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            return {"gates": value}

    action = _latest_queue_action(state)
    failed = _failed_run_report(releases, action)
    if failed:
        request = _request_for(state, str(action.get("request_id") or "")) if action else None
        if request:
            failed["title"] = request.get("approved_topic")
        return failed

    release = _release_to_delivery(releases)
    if not release or not hasattr(releases, "asset_json"):
        return None
    gates: list[dict[str, Any]] = []
    for asset_name in ("quality-final.json", "final-master-qc.json"):
        try:
            document = releases.asset_json(release, asset_name)
        except Exception:
            document = None
        if isinstance(document, dict):
            extracted = _flatten_quality(document)
            for gate in extracted:
                gate["name"] = f"{asset_name}: {gate['name']}"
            gates.extend(extracted)
    if not gates:
        return None
    return {
        "title": str(release.get("name") or release.get("tag_name") or "").strip(),
        "run_id": str(release.get("tag_name") or "").strip(),
        "gates": gates[:30],
    }


def _callback_context(client) -> tuple[str | None, int | None]:
    callback_id = bot_api_10_3.consume_callback_query_id(client)
    if not callback_id:
        return None, None
    raw = panel._read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE")
    try:
        receiver = int(raw)
    except (TypeError, ValueError):
        receiver = None
    return callback_id, receiver


def _send_status_rich(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> None:
    payload = _status_payload(state, releases)
    fallback = panel._status_text(state, releases)
    callback_id, receiver = _callback_context(client)
    rich.send_rich_with_fallback(
        client,
        chat_id,
        rich.production_status_rich_message(payload),
        fallback,
        ephemeral_callback_query_id=callback_id,
        ephemeral_receiver_user_id=receiver,
    )


def _send_last_delivery_rich(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> None:
    delivery = _release_to_delivery(releases)
    if not delivery:
        handler = getattr(active, "_ISCO_RICH_BASE_HANDLE", None)
        if handler is None:
            raise RuntimeError("Telegram rich integration base handler is not installed")
        handler("last_delivery", client, state, releases, chat_id)
        return
    delivery = dict(delivery)
    delivery["files"] = [asset for asset in (delivery.get("assets") or []) if isinstance(asset, dict)]
    fallback = "🎁 آخر إنتاج\n\n" + str(delivery.get("tag_name") or delivery.get("name") or "الحزمة الأخيرة")
    callback_id, receiver = _callback_context(client)
    rich.send_rich_with_fallback(
        client,
        chat_id,
        rich.last_delivery_rich_message(delivery),
        fallback,
        ephemeral_callback_query_id=callback_id,
        ephemeral_receiver_user_id=receiver,
    )


def _send_quality_rich_if_present(client, state: dict[str, Any], releases: Any, chat_id: int | str) -> bool:
    """Explicit diagnostic surface only; status refresh never calls this helper."""
    report = _quality_payload(state, releases)
    if not report:
        return False
    fallback = "🧪 Quality Gates\n\n" + str(report)
    rich.send_rich_with_fallback(client, chat_id, rich.quality_gates_rich_message(report), fallback)
    return True


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "status":
        # One user action -> one status surface. Quality details remain available to
        # explicit diagnostic callers instead of generating a second message on every refresh.
        _send_status_rich(client, state, releases, chat_id)
        return
    if kind == "last_delivery":
        _send_last_delivery_rich(client, state, releases, chat_id)
        return
    handler = getattr(active, "_ISCO_RICH_BASE_HANDLE", None)
    if handler is None:
        raise RuntimeError("Telegram rich integration base handler is not installed")
    handler(kind, client, state, releases, chat_id)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    if not hasattr(active, "_ISCO_RICH_BASE_HANDLE"):
        active._ISCO_RICH_BASE_HANDLE = active._handle_command
    active._handle_command = _handle_command
    _INSTALLED = True
