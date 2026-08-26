from __future__ import annotations

from typing import Any, Iterable

from scripts import telegram_control_panel as panel


_STATUS_LABELS = {
    "research": "البحث",
    "planning": "التخطيط",
    "writing": "الكتابة",
    "voice": "الصوت",
    "audio": "الصوت",
    "editing": "المونتاج",
    "render": "التصدير",
    "quality": "فحص الجودة",
    "quality_gates": "فحص الجودة",
    "delivery": "الحزمة النهائية",
    "complete": "مكتمل",
    "completed": "مكتمل",
    "failed": "فشل",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _stage_label(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return "غير محدد"
    return _STATUS_LABELS.get(raw.casefold(), raw)


def _as_gate_rows(gates: Any) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    if isinstance(gates, dict):
        source: Iterable[tuple[Any, Any]] = gates.items()
    elif isinstance(gates, list):
        source = enumerate(gates, 1)
    else:
        return rows
    for key, item in source:
        name = _clean(key)
        status = ""
        detail = ""
        if isinstance(item, dict):
            name = _clean(item.get("name") or item.get("gate") or item.get("label") or name)
            status = _clean(item.get("status") or item.get("result"))
            detail = _clean(item.get("detail") or item.get("message") or item.get("reason"))
            if not status and "passed" in item:
                status = "pass" if bool(item.get("passed")) else "fail"
        else:
            status = _clean(item)
        if name:
            rows.append((name, status, detail))
    return rows


def _gate_icon(status: str) -> str:
    value = status.casefold()
    if value in {"pass", "passed", "ok", "success", "true", "green"}:
        return "✅"
    if value in {"fail", "failed", "error", "false", "red"}:
        return "❌"
    if value in {"warn", "warning", "yellow"}:
        return "⚠️"
    return "•"


def production_status_rich_message(status: dict[str, Any]) -> dict[str, Any]:
    stage = _stage_label(status.get("stage") or status.get("phase") or status.get("status"))
    progress = status.get("progress")
    if isinstance(progress, (int, float)):
        progress_text = f"{max(0, min(100, int(round(float(progress)))))}%"
    else:
        progress_text = _clean(progress)

    title = _clean(status.get("title") or status.get("topic") or status.get("approved_topic"))
    run_id = _clean(status.get("run_id") or status.get("request_id"))
    note = _clean(status.get("message") or status.get("detail") or status.get("note"))

    blocks: list[dict[str, Any]] = [
        {"type": "heading", "size": 2, "text": "🎛 حالة الإنتاج"},
        {"type": "paragraph", "text": f"المرحلة الحالية: {stage}" + (f" · {progress_text}" if progress_text else "")},
    ]
    if title:
        blocks.append({"type": "paragraph", "text": f"🎯 {title}"})
    if run_id:
        blocks.append({"type": "paragraph", "text": f"🆔 {run_id}"})
    if note:
        blocks.append({"type": "details", "summary": "📋 تفاصيل الحالة", "blocks": [{"type": "paragraph", "text": note}]})

    blocks.append(
        {
            "type": "buttons",
            "align": "right",
            "buttons": [
                {"text": f"⏳ {stage}", "style": "primary", "disabled": {}},
                {"text": "🔄 تحديث", "callback_data": "cmd:status"},
                {"text": "🏠 الرئيسية", "style": "link", "callback_data": "cmd:menu"},
            ],
        }
    )
    return {"blocks": blocks, "is_rtl": True, "skip_entity_detection": True}


def quality_gates_rich_message(report: dict[str, Any]) -> dict[str, Any]:
    gates = _as_gate_rows(report.get("gates") or report.get("quality_gates") or report.get("checks"))
    passed = sum(1 for _, status, _ in gates if _gate_icon(status) == "✅")
    failed = sum(1 for _, status, _ in gates if _gate_icon(status) == "❌")
    title = _clean(report.get("title") or report.get("topic"))
    error = _clean(report.get("error") or report.get("failure") or report.get("reason"))
    run_id = _clean(report.get("run_id") or report.get("request_id"))

    blocks: list[dict[str, Any]] = [
        {"type": "heading", "size": 2, "text": "🧪 Quality Gates"},
        {"type": "paragraph", "text": f"✅ ناجحة: {passed} · ❌ فاشلة: {failed}"},
    ]
    if title:
        blocks.append({"type": "paragraph", "text": f"🎯 {title}"})
    for name, status, detail in gates:
        icon = _gate_icon(status)
        if detail:
            blocks.append({"type": "details", "summary": f"{icon} {name}", "blocks": [{"type": "paragraph", "text": detail}]})
        else:
            blocks.append({"type": "paragraph", "text": f"{icon} {name}"})
    if error:
        blocks.append({"type": "details", "summary": "❌ سبب الفشل", "blocks": [{"type": "paragraph", "text": error}]})

    buttons: list[dict[str, Any]] = [{"text": "🏠 الرئيسية", "style": "link", "callback_data": "cmd:menu"}]
    if run_id:
        buttons.insert(0, {"text": "🔄 تحديث الحالة", "callback_data": "cmd:status"})
    blocks.append({"type": "buttons", "align": "right", "buttons": buttons})
    blocks.append({"type": "footer", "text": "لا يوجد زر يصلح الإنتاج تلقائيًا أو يتجاوز الحواجز. أي إعادة تشغيل تبقى عبر مسار التحكم المعتاد."})
    return {"blocks": blocks, "is_rtl": True, "skip_entity_detection": True}


def last_delivery_rich_message(delivery: dict[str, Any]) -> dict[str, Any]:
    title = _clean(delivery.get("title") or delivery.get("topic") or delivery.get("approved_topic"))
    release = _clean(delivery.get("release") or delivery.get("release_id") or delivery.get("run_id"))
    files = delivery.get("files") or delivery.get("artifacts") or delivery.get("documents") or []
    if isinstance(files, dict):
        files = list(files.values())

    blocks: list[dict[str, Any]] = [
        {"type": "heading", "size": 2, "text": "🎁 آخر إنتاج"},
    ]
    if title:
        blocks.append({"type": "paragraph", "text": f"🎯 {title}"})
    if release:
        blocks.append({"type": "paragraph", "text": f"📦 {release}"})

    added = 0
    for item in files if isinstance(files, list) else []:
        file_id = ""
        name = ""
        if isinstance(item, dict):
            file_id = _clean(item.get("telegram_file_id") or item.get("file_id"))
            name = _clean(item.get("name") or item.get("filename") or item.get("label"))
        else:
            file_id = _clean(item)
        if not file_id:
            continue
        block: dict[str, Any] = {"type": "document", "document": file_id}
        if name:
            block["caption"] = name
        blocks.append(block)
        added += 1
        if added >= 20:
            break

    if not added:
        url = _clean(delivery.get("url") or delivery.get("release_url") or delivery.get("artifact_url"))
        if url:
            blocks.append({"type": "paragraph", "text": f"🔗 {url}"})
        else:
            blocks.append({"type": "paragraph", "text": "لا توجد ملفات Telegram محفوظة مع آخر حزمة؛ ستظهر المرفقات هنا تلقائيًا عندما تتوفر file_id صالحة."})

    blocks.append(
        {
            "type": "buttons",
            "align": "right",
            "buttons": [
                {"text": "🔄 تحديث", "callback_data": "cmd:last_delivery"},
                {"text": "🏠 الرئيسية", "style": "link", "callback_data": "cmd:menu"},
            ],
        }
    )
    return {"blocks": blocks, "is_rtl": True, "skip_entity_detection": True}


def ephemeral_callback_parameters(callback_query_id: str | None) -> dict[str, Any] | None:
    value = _clean(callback_query_id)
    if not value:
        return None
    return {"callback_query_id": value, "replace_callback_query_message": True}


def send_rich_with_fallback(client, chat_id: int | str, rich_message: dict[str, Any], fallback_text: str, *, ephemeral_callback_query_id: str | None = None):
    payload: dict[str, Any] = {"chat_id": chat_id, "rich_message": rich_message}
    ephemeral = ephemeral_callback_parameters(ephemeral_callback_query_id)
    if ephemeral:
        payload["ephemeral_message_parameters"] = ephemeral
    try:
        return client.call("sendRichMessage", payload)
    except Exception:
        return client.send(chat_id, fallback_text)
