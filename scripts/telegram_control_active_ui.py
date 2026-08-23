from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts import telegram_control_panel as panel
from scripts import telegram_control_simple_ui as simple
from scripts.telegram_production_queue import enqueue_latest_request, pending_dispatch

_BASE_POLL = panel.poll


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


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "produce_latest":
        if not _production_enabled():
            client.send(chat_id, "🔒 إنتاج Telegram مقفول حاليًا.", keyboard=_main_keyboard())
            return
        status, action = enqueue_latest_request(state, chat_id=chat_id)
        if status == "no_ready_request":
            text = "لا يوجد قرار معتمد جاهز للإنتاج. اختر «✨ اقترح» واعتمد موضوعًا أولًا."
        elif status == "already_queued":
            text = "⏳ طلب الإنتاج المعتمد موجود بالفعل في طابور الإرسال. لن أكرر التشغيل."
        elif status == "already_dispatched_recent":
            text = "✅ تم إرسال هذا الطلب للإنتاج بالفعل مؤخرًا. لن أنشئ محاولة مكررة."
        elif status == "retry_queued":
            text = "🚀 أعدت وضع الطلب المعتمد في طابور التشغيل بعد انتهاء نافذة الحماية من التكرار."
        else:
            text = (
                "🚀 تم تأكيد بدء الإنتاج للقرار المعتمد.\n"
                "سيُرسل الطلب مرة واحدة إلى مسار الإنتاج المحمي، وستصلك التحديثات في Telegram."
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
    panel._handle_command = _handle_command
    panel.poll = _poll


def main() -> None:
    _install()
    panel.main()


if __name__ == "__main__":
    main()
