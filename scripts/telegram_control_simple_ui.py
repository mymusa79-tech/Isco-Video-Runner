from __future__ import annotations

import sys

from scripts import telegram_control_panel as panel


_BASE_HANDLE_COMMAND = panel._handle_command


def _main_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "✨ اقترح", "callback_data": "cmd:suggest"}],
        [{"text": "🎁 آخر إنتاج", "callback_data": "cmd:last_delivery"}],
        [{"text": "🧭 الحالة", "callback_data": "cmd:status"}],
    ]


def _menu_text() -> str:
    return (
        "🎛 نداء اليقظة\n\n"
        "اختر ما تحتاجه فقط. التفاصيل تظهر عند طلبها.\n\n"
        "🔒 لا نشر إلى YouTube من هذه اللوحة.\n"
        "الرفع والنشر والجدولة تبقى يدويًا بيدك داخل YouTube Studio."
    )


def _delivery_keyboard(long_release: dict | None, short_release: dict | None) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    if long_release and long_release.get("html_url"):
        rows.append([{"text": "🎬 فتح حزمة الفيديو", "url": str(long_release["html_url"])}])
        tag = str(long_release.get("tag_name") or "")
        if tag and len(tag) <= 35:
            rows.append([{"text": "🅰️ عناوين وصور A/B/C", "callback_data": f"pack:{tag}"}])
    if short_release and short_release.get("html_url"):
        rows.append([{"text": "📱 فتح آخر شورت", "url": str(short_release["html_url"])}])
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
    return rows


def _last_delivery_text(long_release: dict | None, short_release: dict | None) -> str:
    lines = ["🎁 آخر إنتاج", ""]
    if long_release:
        lines.append(f"🎬 الفيديو: {long_release.get('tag_name') or 'جاهز'}")
        lines.append(f"   {str(long_release.get('published_at') or long_release.get('created_at') or '')[:10] or 'تاريخ غير معروف'}")
    else:
        lines.append("🎬 الفيديو: لا يوجد تسليم بعد")
    if short_release:
        lines.append(f"📱 الشورت: {short_release.get('tag_name') or 'جاهز'}")
        lines.append(f"   {str(short_release.get('published_at') or short_release.get('created_at') or '')[:10] or 'تاريخ غير معروف'}")
    else:
        lines.append("📱 الشورت: لا يوجد تسليم بعد")
    if long_release or short_release:
        lines.extend(["", "افتح الحزمة فقط عندما تحتاج الملفات أو خيارات A/B/C."])
    lines.extend(["", "YouTube: نشر يدوي فقط."])
    return "\n".join(lines)


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "menu":
        client.send(chat_id, _menu_text(), keyboard=_main_keyboard())
        return
    if kind == "suggest":
        client.send(
            chat_id,
            "✨ ماذا تريد أن أقترح؟",
            keyboard=[
                [{"text": "🎬 حلقة", "callback_data": "cmd:topic"}],
                [{"text": "📱 شورت", "callback_data": "cmd:short"}],
                [{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}],
            ],
        )
        return
    if kind == "last_delivery":
        long_release = releases.latest("video-")
        short_release = releases.latest("short-")
        client.send(
            chat_id,
            _last_delivery_text(long_release, short_release),
            keyboard=_delivery_keyboard(long_release, short_release),
        )
        return
    if kind == "status":
        text = panel._status_text(state, releases)
        text += "\n\n🔒 نشر YouTube: يدوي فقط"
        client.send(chat_id, text, keyboard=_main_keyboard())
        return
    _BASE_HANDLE_COMMAND(kind, client, state, releases, chat_id)


def _install() -> None:
    panel._main_keyboard = _main_keyboard
    panel._menu_text = _menu_text
    panel._handle_command = _handle_command


def main() -> None:
    _install()
    panel.main()


if __name__ == "__main__":
    main()
