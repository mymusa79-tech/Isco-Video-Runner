from __future__ import annotations

"""Final Telegram operator-state presentation layer.

This module owns presentation and receipt normalization only. It does not create a
new Production authority path: the exact typed confirmation still delegates to the
already-certified active control handler, including its immutable request/hash
validation and durable dispatch ledger. Mission Control only projects that internal
ledger into operator lifecycle states so retry protection is never presented as a
user-visible queue.
"""

from dataclasses import dataclass
from typing import Any, Callable

from scripts import telegram_control_active_ui as active
from scripts import telegram_creator_control_center_v5 as creator_v5
from scripts import telegram_persistent_control_ui as persistent_ui
from scripts.telegram_production_queue import live_production_dispatches


@dataclass(frozen=True)
class ProductionOperatorState:
    lifecycle: str
    label: str
    stage: str
    request_id: str
    topic: str
    workflow_run_id: str
    updated_at: str
    attempt: int


_INSTALLED = False
_BASE_OPERATOR_STATUS: Callable[..., Any] | None = None
_BASE_SYSTEM_STATUS: Callable[..., Any] | None = None
_BASE_PRODUCTION_HANDLE: Callable[..., Any] | None = None


def _request_for(state: dict[str, Any], request_id: str) -> dict[str, Any]:
    requests = state.get("requests")
    if not isinstance(requests, dict):
        return {}
    request = requests.get(request_id)
    return request if isinstance(request, dict) else {}


def _topic_for(state: dict[str, Any], request_id: str) -> str:
    request = _request_for(state, request_id)
    return str(request.get("approved_topic") or request.get("topic") or "").strip()


def _action_updated_at(action: dict[str, Any]) -> str:
    for key in ("completed_at", "failed_at", "consumed_at", "reserved_at", "requested_at"):
        value = str(action.get(key) or "").strip()
        if value:
            return value
    return ""


def _matching_actions(state: dict[str, Any], request_id: str, request_sha256: str) -> list[dict[str, Any]]:
    raw = state.get("production_queue")
    if not isinstance(raw, list):
        return []
    return [
        item
        for item in raw
        if isinstance(item, dict)
        and str(item.get("request_id") or "") == request_id
        and str(item.get("request_sha256") or "") == request_sha256
    ]


def _snapshot_from_action(state: dict[str, Any], action: dict[str, Any]) -> ProductionOperatorState:
    status = str(action.get("status") or "")
    request_id = str(action.get("request_id") or "").strip()
    if status == "pending_dispatch":
        lifecycle = "starting"
        label = "🔵 بدء التشغيل"
        stage = "تسجيل التفويض وتسليمه إلى V4"
    elif status == "dispatch_reserved":
        lifecycle = "starting"
        label = "🔵 بدء التشغيل"
        stage = "حُجز التفويض لمرة واحدة ويجري تسليمه إلى V4"
    elif status == "dispatch_consumed":
        lifecycle = "running"
        label = "🎬 قيد التشغيل"
        stage = "V4 الموحد يعمل الآن"
    elif status == "completed":
        lifecycle = "completed"
        label = "✅ مكتمل"
        stage = "اكتمل Production لهذا الطلب"
    elif status == "failed" and str(action.get("failure_reason") or "") == "production_cancelled":
        lifecycle = "cancelled"
        label = "⚪ ملغي"
        stage = "أُلغي Production لهذا الطلب"
    else:
        lifecycle = "failed"
        label = "🔴 فشل"
        stage = "انتهت محاولة Production بالفشل"
    try:
        attempt = int(action.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    return ProductionOperatorState(
        lifecycle=lifecycle,
        label=label,
        stage=stage,
        request_id=request_id,
        topic=_topic_for(state, request_id),
        workflow_run_id=str(action.get("workflow_run_id") or "").strip(),
        updated_at=_action_updated_at(action),
        attempt=max(1, attempt),
    )


def current_production_state(state: dict[str, Any]) -> ProductionOperatorState | None:
    """Return one canonical operator state for the current Production transaction.

    Live dispatch ledger entries always win over an approval target because the
    target intentionally remains durable after activation. When no live Production
    exists, a terminal attempt bound to the same target wins over the approval card;
    only a target with no terminal attempt projects as awaiting confirmation.
    """

    live = live_production_dispatches(state)
    if live:
        return _snapshot_from_action(state, live[-1])

    target = active._current_target(state)
    if target is None:
        return None
    request_id = str(target.get("request_id") or "").strip()
    request_sha256 = str(target.get("request_sha256") or "").strip()
    matches = _matching_actions(state, request_id, request_sha256)
    if matches and str(matches[-1].get("status") or "") in {"completed", "failed"}:
        return _snapshot_from_action(state, matches[-1])

    raw_target = state.get(active.PRODUCTION_TARGET_KEY)
    updated_at = ""
    if isinstance(raw_target, dict):
        updated_at = str(raw_target.get("selected_at") or "").strip()
    return ProductionOperatorState(
        lifecycle="awaiting_confirmation",
        label="🟡 ينتظر التأكيد",
        stage="لم يبدأ Production",
        request_id=request_id,
        topic=_topic_for(state, request_id),
        workflow_run_id="",
        updated_at=updated_at,
        attempt=0,
    )


def _run_line(snapshot: ProductionOperatorState) -> str:
    if snapshot.workflow_run_id:
        return f"Production Run ID: {snapshot.workflow_run_id}"
    if snapshot.lifecycle == "awaiting_confirmation":
        return "Production Run: لم يبدأ بعد"
    if snapshot.lifecycle == "starting":
        return "Production Run: لم يُسجّل بعد"
    if snapshot.lifecycle == "running":
        return "Production Run: أُرسل إلى V4؛ رقم التشغيل غير مسجل بعد"
    if snapshot.lifecycle == "completed":
        return "Production Run: مكتمل"
    if snapshot.lifecycle == "cancelled":
        return "Production Run: أُلغي"
    return "Production Run: فشل"


def render_operator_status(snapshot: ProductionOperatorState) -> str:
    lines = ["🧭 الحالة — مركز التشغيل", ""]
    if snapshot.topic:
        lines.append(f"🎯 الموضوع: {snapshot.topic}")
    if snapshot.request_id:
        lines.append(f"🆔 الطلب: {snapshot.request_id}")
    lines.extend(
        [
            "",
            "🎬 Production",
            f"الحالة: {snapshot.label}",
            f"المرحلة: {snapshot.stage}",
            _run_line(snapshot),
        ]
    )
    if snapshot.attempt > 1:
        lines.append(f"محاولة التشغيل: {snapshot.attempt}")
    if snapshot.updated_at:
        lines.append(f"آخر تحديث: {snapshot.updated_at}")
    lines.extend(["", "مطلوب منك"])
    if snapshot.lifecycle == "awaiting_confirmation":
        lines.append(f"إذا كان القرار نهائيًا، أرسل حرفيًا: {persistent_ui.PRODUCTION_CONFIRMATION_TEXT}")
    elif snapshot.lifecycle in {"failed", "cancelled"}:
        lines.append(f"لإعادة المحاولة لنفس الطلب، أرسل حرفيًا: {persistent_ui.PRODUCTION_CONFIRMATION_TEXT}")
    elif snapshot.lifecycle == "completed":
        lines.append("لا شيء الآن — افتح «آخر إنتاج» لمراجعة الحزمة.")
    else:
        lines.append(f"لا شيء الآن — لا تكرر «{persistent_ui.PRODUCTION_CONFIRMATION_TEXT}».")
    lines.extend(["", "🔒 YouTube: النشر يبقى يدويًا فقط."])
    return "\n".join(lines)


def _operator_status(state: dict[str, Any], releases: Any):
    if _BASE_OPERATOR_STATUS is None:
        raise RuntimeError("Telegram operator mission control is not installed")

    # Research owns the operator's immediate attention while it is pending. Do not
    # hide that behind an older approved target card.
    pending = creator_v5.research_status.pending_research(state)
    if pending is not None:
        return _BASE_OPERATOR_STATUS(state, releases)

    snapshot = current_production_state(state)
    if snapshot is None:
        return _BASE_OPERATOR_STATUS(state, releases)

    _, rows = _BASE_OPERATOR_STATUS(state, releases)
    return render_operator_status(snapshot), rows


def _system_status(state: dict[str, Any], releases: Any):
    if _BASE_SYSTEM_STATUS is None:
        raise RuntimeError("Telegram operator mission control is not installed")
    text, rows = _BASE_SYSTEM_STATUS(state, releases)
    body = str(text or "")
    body = body.replace("Run #", "تشغيل تشخيصي #")
    body = body.replace("\n\nهذه شاشة تشخيص فقط؛ لا تغيّر Production أو Quality Gates.", "")
    text = (
        "🧪 تفاصيل النظام — تشخيص تقني\n"
        "هذه ليست حالة Production الحالية، وأي تشغيل ظاهر هنا تشخيصي ما لم يُذكر صراحة أنه Production Run.\n\n"
        f"{body}\n\n"
        "ℹ️ هذه الشاشة للقراءة فقط؛ لا تغيّر Production أو Quality Gates."
    )
    return text, rows


def _receipt_text(status: str, action: dict[str, Any] | None) -> str:
    if status == "already_queued":
        text = (
            "🔵 نفس عملية الإنتاج في مرحلة بدء التشغيل بالفعل.\n"
            "حماية التكرار منعت إنشاء عملية ثانية لنفس التأكيد."
        )
    elif status == "already_reserved_recent":
        text = (
            "🔵 نفس عملية الإنتاج في مرحلة بدء التشغيل وقد حُجز تفويضها لمرة واحدة.\n"
            "لن تُنشأ عملية ثانية لنفس التأكيد."
        )
    elif status == "already_dispatched_recent":
        text = "🎬 نفس Production Run بدأ بالفعل. لن تُنشأ نسخة ثانية لنفس التأكيد."
    elif status == "already_completed":
        text = "✅ هذا الطلب اكتمل إنتاجه سابقًا. لن تبدأ نسخة أخرى منه."
    elif status == "retry_queued":
        text = (
            "🚀 تم قبول محاولة تشغيل جديدة للطلب نفسه بعد انتهاء المحاولة السابقة.\n"
            "الحالة: 🔵 بدء التشغيل\n"
            "حماية التكرار تمنع النسخ المتزامنة فقط، وليست مرحلة انتظار."
        )
    else:
        text = (
            "🚀 تم تأكيد الإنتاج للاختيار الحالي.\n"
            "الحالة: 🔵 بدء التشغيل\n"
            "يُسلَّم التفويض مرة واحدة إلى مسار V4 المحمي."
        )
    if action is not None:
        request_id = str(action.get("request_id") or "").strip()
        if request_id:
            text += f"\n\nرقم الطلب: {request_id}"
        workflow_run_id = str(action.get("workflow_run_id") or "").strip()
        if workflow_run_id:
            text += f"\nProduction Run ID: {workflow_run_id}"
        elif status not in {"already_completed"}:
            text += "\nProduction Run: لم يُسجّل بعد"
    text += "\n\nYouTube: النشر يبقى يدويًا فقط."
    return text


class _ReceiptCaptureClient:
    """Capture only the base handler's Telegram sends while delegating everything else."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.sent: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def send(self, *args, **kwargs) -> None:
        self.sent.append((args, kwargs))

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)


def _classify_confirmation_result(
    state: dict[str, Any],
    target: dict[str, str] | None,
    before_queue_length: int,
) -> tuple[str, dict[str, Any]] | None:
    if target is None:
        return None
    matches = _matching_actions(
        state,
        str(target.get("request_id") or ""),
        str(target.get("request_sha256") or ""),
    )
    if not matches:
        return None
    action = matches[-1]
    status = str(action.get("status") or "")
    raw_queue = state.get("production_queue")
    after_queue_length = len(raw_queue) if isinstance(raw_queue, list) else 0
    if status == "pending_dispatch":
        if after_queue_length > before_queue_length:
            try:
                attempt = int(action.get("attempt") or 1)
            except (TypeError, ValueError):
                attempt = 1
            return ("retry_queued" if attempt > 1 else "queued", action)
        return "already_queued", action
    if status == "dispatch_reserved":
        return "already_reserved_recent", action
    if status == "dispatch_consumed":
        return "already_dispatched_recent", action
    if status == "completed":
        return "already_completed", action
    return None


def _replay_capture(client, captured: _ReceiptCaptureClient) -> None:
    for args, kwargs in captured.sent:
        client.send(*args, **kwargs)


def _send_normalized_receipt(client, captured: _ReceiptCaptureClient, fallback_chat_id, text: str) -> None:
    if not captured.sent:
        client.send(fallback_chat_id, text, keyboard=creator_v5._main_keyboard())
        return
    args, kwargs = captured.sent[-1]
    out_chat_id = args[0] if args else kwargs.get("chat_id", fallback_chat_id)
    keyboard = kwargs.get("keyboard")
    if keyboard is None and len(args) >= 3:
        keyboard = args[2]
    client.send(out_chat_id, text, keyboard=keyboard)


def _production_handle(kind, client, state: dict[str, Any], releases: Any, chat_id) -> None:
    """Delegate authority unchanged, then normalize only the resulting receipt."""

    if _BASE_PRODUCTION_HANDLE is None:
        raise RuntimeError("Telegram operator mission control is not installed")
    if kind != "produce_latest":
        _BASE_PRODUCTION_HANDLE(kind, client, state, releases, chat_id)
        return

    target_before = active._current_target(state)
    raw_queue = state.get("production_queue")
    before_queue_length = len(raw_queue) if isinstance(raw_queue, list) else 0
    captured = _ReceiptCaptureClient(client)

    # This is the sole authority call. All production-enabled checks, target/hash
    # validation, enqueue/idempotency behavior and fail-closed mutations stay in the
    # pre-existing certified active handler.
    _BASE_PRODUCTION_HANDLE(kind, captured, state, releases, chat_id)

    classified = _classify_confirmation_result(state, target_before, before_queue_length)
    if classified is None:
        _replay_capture(client, captured)
        return
    status, action = classified
    _send_normalized_receipt(client, captured, chat_id, _receipt_text(status, action))


def install() -> None:
    """Install after V5/session-continuity as the final operator presentation seam."""

    global _INSTALLED, _BASE_OPERATOR_STATUS, _BASE_SYSTEM_STATUS, _BASE_PRODUCTION_HANDLE
    if _INSTALLED:
        return
    _BASE_OPERATOR_STATUS = creator_v5._operator_status
    _BASE_SYSTEM_STATUS = creator_v5._system_status
    _BASE_PRODUCTION_HANDLE = persistent_ui._BASE_HANDLE_COMMAND
    creator_v5._operator_status = _operator_status
    creator_v5._system_status = _system_status
    # persistent_ui._handle_command dereferences this module global at call time.
    # Rebinding it preserves the exact typed-confirmation authority boundary while
    # replacing only the post-handler receipt shown to the operator.
    persistent_ui._BASE_HANDLE_COMMAND = _production_handle
    _INSTALLED = True
