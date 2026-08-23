from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts import telegram_control_panel as panel
from scripts import telegram_control_simple_ui as simple
from scripts.telegram_production_queue import enqueue_request, pending_dispatch

_BASE_POLL = panel.poll
PRODUCTION_TARGET_KEY = "production_target"
ACTIVE_RESEARCH_SESSION_KEY = "active_research_session_id"


def _production_enabled() -> bool:
    return (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() == "true"


def _main_keyboard() -> list[list[dict[str, str]]]:
    rows = list(simple._main_keyboard())
    if _production_enabled():
        rows.insert(1, [{"text": "🚀 ابدأ الإنتاج المعتمد", "callback_data": "cmd:produce_latest"}])
    return rows


def _menu_text() -> str:
    if _production_enabled():
        production = (
            "🟢 إنتاج Telegram مفعّل.\n"
            "لا يبدأ شيء بمجرد اعتماد الموضوع؛ التشغيل يحتاج ضغطة مستقلة على «🚀 ابدأ الإنتاج المعتمد»."
        )
    else:
        production = "🔒 إنتاج Telegram مقفول حاليًا."
    return (
        "🎛 نداء اليقظة\n\n"
        "اختر ما تحتاجه فقط. التفاصيل تظهر عند طلبها.\n\n"
        f"{production}\n\n"
        "🔒 لا نشر إلى YouTube من هذه اللوحة.\n"
        "الرفع والنشر والجدولة تبقى يدويًا بيدك داخل YouTube Studio."
    )


def _approval_text(request: dict[str, Any]) -> str:
    if request.get("approval_scope") == "long_plus_sibling_shorts":
        scope = "حلقة طويلة + 2–3 Shorts مختلفة حسب المادة"
    elif request.get("approval_scope") in {"short_only", "short_sibling"}:
        scope = "Short فقط"
    else:
        scope = "حلقة طويلة فقط"
    if _production_enabled():
        action = (
            "لم يبدأ الإنتاج بعد.\n"
            "إذا كان القرار نهائيًا اضغط «🚀 ابدأ الإنتاج المعتمد» من اللوحة."
        )
    else:
        action = "🔒 لم يبدأ أي Production Run. التفعيل مقفول حاليًا."
    return (
        "✅ تم اعتماد القرار وحفظه\n\n"
        f"الموضوع: {request.get('approved_topic', '')}\n"
        f"النطاق: {scope}\n"
        f"رقم الطلب: {request.get('request_id', '')}\n\n"
        f"{action}"
    )


def _clear_current_selection(state: dict[str, Any]) -> None:
    state.pop(PRODUCTION_TARGET_KEY, None)
    state.pop(ACTIVE_RESEARCH_SESSION_KEY, None)


def _current_target(state: dict[str, Any]) -> dict[str, str] | None:
    target = state.get(PRODUCTION_TARGET_KEY)
    if not isinstance(target, dict):
        return None
    request_id = str(target.get("request_id") or "").strip()
    request_sha256 = str(target.get("request_sha256") or "").strip()
    session_id = str(target.get("session_id") or "").strip()
    active_session = str(state.get(ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not request_id or not request_sha256 or not session_id or session_id != active_session:
        return None
    return {
        "request_id": request_id,
        "request_sha256": request_sha256,
        "session_id": session_id,
    }


def _approve_current(
    state: dict[str, Any], session: dict[str, Any], index: int, scope: str
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "").strip()
    active_session = str(state.get(ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not session_id or session_id != active_session:
        raise RuntimeError("This Telegram selection is not from the current research session")
    request = simple._approve(state, session, index, scope)
    state[PRODUCTION_TARGET_KEY] = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": str(request.get("request_sha256") or ""),
        "session_id": session_id,
        "selected_at": str(request.get("approved_at") or panel._now()),
    }
    return request


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind in {"topic", "short"}:
        _clear_current_selection(state)
        simple._handle_command(kind, client, state, releases, chat_id)
        return
    if kind == "produce_latest":
        if not _production_enabled():
            client.send(chat_id, "🔒 إنتاج Telegram مقفول حاليًا.", keyboard=_main_keyboard())
            return
        target = _current_target(state)
        if target is None:
            client.send(
                chat_id,
                "لا يوجد اختيار صالح من جلسة البحث الحالية. اضغط «✨ اقترح»، انتظر 3 خيارات جديدة، ثم اختر واحدًا قبل بدء الإنتاج.",
                keyboard=_main_keyboard(),
            )
            return
        status, action = enqueue_request(
            state,
            target["request_id"],
            target["request_sha256"],
            chat_id=chat_id,
        )
        if status == "no_ready_request":
            state.pop(PRODUCTION_TARGET_KEY, None)
            text = "الاختيار الحالي لم يعد صالحًا للإنتاج. اطلب 3 خيارات جديدة واختر من بينها مرة أخرى."
        elif status == "already_queued":
            text = "⏳ طلب الإنتاج المعتمد موجود بالفعل في طابور الإرسال. لن أكرر التشغيل."
        elif status == "already_reserved_recent":
            text = "🔒 تم حجز هذا الطلب للتشغيل بالفعل. لن أعيد إرساله تلقائيًا؛ أي محاولة لاحقة تحتاج إجراءً صريحًا جديدًا."
        elif status == "already_dispatched_recent":
            text = "✅ تم إرسال هذا الطلب للإنتاج بالفعل مؤخرًا. لن أنشئ محاولة مكررة."
        elif status == "retry_queued":
            text = "🚀 أعدت وضع الاختيار الحالي في طابور التشغيل بعد انتهاء نافذة الحماية من التكرار."
        else:
            text = (
                "🚀 تم تأكيد بدء الإنتاج للاختيار الحالي.\n"
                "سيُحجز القرار أولًا داخل الحالة المشفرة، ثم يُرسل مرة واحدة إلى مسار الإنتاج المحمي."
            )
        if action is not None:
            text += f"\n\nرقم الطلب: {action.get('request_id')}"
        text += "\n\nYouTube: النشر يبقى يدويًا فقط."
        client.send(chat_id, text, keyboard=_main_keyboard())
        return
    if kind == "status":
        text = panel._status_text(state, releases)
        text += "\n\n🟢 إنتاج Telegram: مفعّل بضغطة تشغيل مستقلة" if _production_enabled() else "\n\n🔒 إنتاج Telegram: مقفول"
        text += "\n🔒 نشر YouTube: يدوي فقط"
        client.send(chat_id, text, keyboard=_main_keyboard())
        return
    simple._handle_command(kind, client, state, releases, chat_id)


def _research_current(state_path: Path) -> None:
    before = panel.load_state(state_path)
    before_ids = set(before.get("sessions") or {})
    simple._research(state_path)
    state = panel.load_state(state_path)
    sessions = state.get("sessions") or {}
    new_ids = [session_id for session_id in sessions if session_id not in before_ids]
    if len(new_ids) != 1:
        _clear_current_selection(state)
        panel.save_state(state_path, state)
        raise RuntimeError("Current Telegram research did not create exactly one fresh selection session")
    state[ACTIVE_RESEARCH_SESSION_KEY] = new_ids[0]
    state.pop(PRODUCTION_TARGET_KEY, None)
    panel.save_state(state_path, state)


def _poll(state_path: Path) -> None:
    _BASE_POLL(state_path)
    state = panel.load_state(state_path)
    action = pending_dispatch(state) if _production_enabled() else None
    panel._github_output("needs_production", "true" if action is not None else "false")
    panel._github_output("production_request_id", str(action.get("request_id") or "") if action else "")
    panel._github_output("production_request_sha256", str(action.get("request_sha256") or "") if action else "")
    panel._github_output("production_release_tag", str(action.get("release_tag") or "") if action else "")


def _install() -> None:
    simple._install()
    panel._main_keyboard = _main_keyboard
    panel._menu_text = _menu_text
    panel._approval_text = _approval_text
    panel._approve = _approve_current
    panel._handle_command = _handle_command
    panel.research = _research_current
    panel.poll = _poll


def main() -> None:
    _install()
    panel.main()


if __name__ == "__main__":
    main()
