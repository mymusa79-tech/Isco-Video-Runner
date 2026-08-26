from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_youtube_stats as youtube_stats

START_BUTTON_TEXT = "🏠 ابدأ"
LEGACY_START_BUTTON_TEXT = "🎛 ابدأ"
PRODUCTION_CONFIRMATION_TEXT = "تأكيد الإنتاج"
PERSISTENT_SURFACE_VERSION = 3
PERSISTENT_SURFACE_STATE_KEY = "telegram_persistent_start_surface_version"

_BASE_COMMAND_KIND = active._command_kind
_BASE_HANDLE_COMMAND = active._handle_command
_BASE_POLL = active._poll
_INSTALLED = False


def _persistent_reply_markup() -> dict[str, Any]:
    return {
        "keyboard": [[{"text": START_BUTTON_TEXT}]],
        "resize_keyboard": True,
        "is_persistent": True,
        "one_time_keyboard": False,
        "input_field_placeholder": "اضغط «🏠 ابدأ» لفتح لوحة نداء اليقظة",
    }


def _main_keyboard() -> list[list[dict[str, str]]]:
    """Root navigation only. Children are revealed after choosing a section."""
    return [
        [{"text": "1️⃣ 🔎 البحث", "callback_data": "cmd:search_menu"}],
        [{"text": "2️⃣ 📚 المواضيع", "callback_data": "cmd:library_menu"}],
        [{"text": "3️⃣ 🎁 آخر إنتاج", "callback_data": "cmd:last_delivery"}],
        [{"text": "4️⃣ 📊 الحالة", "callback_data": "cmd:status"}],
        [{"text": "5️⃣ 📈 الإحصائيات", "callback_data": "cmd:stats_menu"}],
    ]


def _search_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🎬 بحث حلقة", "callback_data": "cmd:topic"}],
        [{"text": "⚡ بحث شورت", "callback_data": "cmd:short"}],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _library_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "📚 المحفوظة", "callback_data": "cmd:saved"}],
        [{"text": "✅ المستعملة", "callback_data": "cmd:used"}],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _stats_keyboard() -> list[list[dict[str, str]]]:
    return [
        [{"text": "🎬 آخر فيديو", "callback_data": "cmd:stats_last_long"}],
        [{"text": "⚡ آخر Short", "callback_data": "cmd:stats_last_short"}],
        [{"text": "🗓️ اليوم", "callback_data": "cmd:stats_today"}],
        [{"text": "📅 آخر 7 أيام", "callback_data": "cmd:stats_week"}],
        [{"text": "🌐 عامة", "callback_data": "cmd:stats_overview"}],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _menu_text() -> str:
    production = (
        "🔐 بدء الإنتاج لا يظهر كزر هنا؛ بعد اعتماد موضوع محدد اكتب حرفيًا «تأكيد الإنتاج»."
        if active._production_enabled()
        else "🔒 إنتاج Telegram مقفول حاليًا."
    )
    return (
        "🏠 نداء اليقظة\n\n"
        "اختر القسم الذي تحتاجه:\n\n"
        "1️⃣ 🔎 البحث — بحث جديد للحلقة أو الشورت.\n"
        "2️⃣ 📚 المواضيع — المحفوظة والمستعملة.\n"
        "3️⃣ 🎁 آخر إنتاج — آخر حزمة وروابطها.\n"
        "4️⃣ 📊 الحالة — وضع البحث والإنتاج الآن.\n"
        "5️⃣ 📈 الإحصائيات — صورة سريعة ومحدثة عن القناة.\n\n"
        f"{production}\n"
        "🔒 YouTube: الرفع والنشر والجدولة يدويًا فقط."
    )


def _search_menu_text() -> str:
    return (
        "🔎 البحث\n\n"
        "اختر نوع البحث. الاختيار هنا يبدأ البحث فقط ولا يبدأ Production:\n\n"
        "🎬 حلقة — 3 أفكار طويلة مرتبة ومقيّمة.\n"
        "⚡ شورت — 3 أفكار قصيرة مرتبة ومقيّمة."
    )


def _library_menu_text() -> str:
    return (
        "📚 المواضيع\n\n"
        "اختر القائمة التي تريد فتحها:\n\n"
        "📚 المحفوظة — أفكار جيدة لم تُنتج بعد.\n"
        "✅ المستعملة — مواضيع اكتمل إنتاجها ولا تعاد في البحث."
    )


def _stats_menu_text() -> str:
    return (
        "📈 إحصائيات نداء اليقظة\n\n"
        "اختر الصورة التي تريدها. الأرقام تُجلب من YouTube عند الطلب وتُعرض كبوصلة سريعة، لا كتقرير محاسبي دقيق."
    )


def _command_kind(text: str) -> str | None:
    raw = str(text or "").strip()
    if raw == PRODUCTION_CONFIRMATION_TEXT:
        return "confirm_production"
    value = panel._normalize_command(raw)
    mapping = {
        START_BUTTON_TEXT.casefold(): "menu",
        LEGACY_START_BUTTON_TEXT.casefold(): "menu",
        "1": "search_menu",
        "١": "search_menu",
        "1️⃣": "search_menu",
        "1️⃣ 🔎 البحث": "search_menu",
        "2": "library_menu",
        "٢": "library_menu",
        "2️⃣": "library_menu",
        "2️⃣ 📚 المواضيع": "library_menu",
        "3": "last_delivery",
        "٣": "last_delivery",
        "3️⃣": "last_delivery",
        "3️⃣ 🎁 آخر إنتاج": "last_delivery",
        "4": "status",
        "٤": "status",
        "4️⃣": "status",
        "4️⃣ 📊 الحالة": "status",
        "5": "stats_menu",
        "٥": "stats_menu",
        "5️⃣": "stats_menu",
        "5️⃣ 📈 الإحصائيات": "stats_menu",
        "بحث": "search_menu",
        "المواضيع": "library_menu",
        "الإحصائيات": "stats_menu",
        "الاحصائيات": "stats_menu",
    }
    if value in mapping:
        return mapping[value]
    return _BASE_COMMAND_KIND(text)


def _approval_text(request: dict[str, Any]) -> str:
    if request.get("approval_scope") == "long_plus_sibling_shorts":
        scope = "حلقة طويلة + 2–3 Shorts مختلفة حسب المادة"
    elif request.get("approval_scope") in {"short_only", "short_sibling"}:
        scope = "Short فقط"
    else:
        scope = "حلقة طويلة فقط"
    if active._production_enabled():
        action = (
            "⏸️ لم يبدأ الإنتاج بعد.\n"
            "لإطلاق هذا الاختيار فقط، اكتب في رسالة جديدة حرفيًا:\n"
            f"{PRODUCTION_CONFIRMATION_TEXT}\n\n"
            "أي زر تشغيل قديم أو صياغة أخرى لن تبدأ Production."
        )
    else:
        action = "🔒 لم يبدأ أي Production Run. التفعيل مقفول حاليًا."
    return (
        "✅ تم اعتماد القرار وحفظه\n\n"
        f"🎯 الموضوع: {request.get('approved_topic', '')}\n"
        f"📦 النطاق: {scope}\n"
        f"🆔 رقم الطلب: {request.get('request_id', '')}\n\n"
        f"{action}"
    )


def _candidate_keyboard(session_id: str, kind: str) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    pick = "pickshort" if kind == "short" else "pick"
    badges = ("1️⃣", "2️⃣", "3️⃣")
    for index, badge in enumerate(badges):
        rows.append(
            [
                {"text": f"{badge} اختيار", "callback_data": f"{pick}:{session_id}:{index}"},
                {"text": f"🔎 تفاصيل {index + 1}", "callback_data": f"detail:{session_id}:{index}"},
            ]
        )
    rows.append([{"text": "🔄 3 خيارات أخرى", "callback_data": f"refresh:{kind}"}])
    rows.append([{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}])
    return rows


def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    icon = "🎬" if kind == "long" else "⚡"
    heading = "3 خيارات للحلقة" if kind == "long" else "3 خيارات للشورت"
    badges = ("1️⃣", "2️⃣", "3️⃣")
    lines = [f"{icon} {heading}", ""]
    for index, item in enumerate(candidates[:3]):
        badge = badges[index]
        score = float(item.get("control_score", item.get("opportunity_score", 0.0)) or 0.0) * 10
        why = [str(value) for value in (item.get("why") or []) if str(value).strip()]
        lines.append(f"{badge} {str(item.get('title') or '').strip()}")
        lines.append(f"   ⭐ فرصة: {score:.1f}/10")
        if why:
            lines.append("   💡 " + " — ".join(why[:2]))
        lines.append("")
    lines.append("👇 اختر رقمًا، أو افتح تفاصيل الرقم الذي تريد مراجعته.")
    return "\n".join(lines).strip()


def _stale_start_button_text() -> str:
    return (
        "⛔ زر بدء الإنتاج القديم لم يعد صالحًا للتشغيل.\n\n"
        "إذا كان لديك موضوع معتمد حاليًا، اكتب حرفيًا:\n"
        f"{PRODUCTION_CONFIRMATION_TEXT}"
    )


def _status_text(state: dict[str, Any], releases) -> str:
    text = panel._status_text(state, releases)
    text += f"\n📚 محفوظة: {len(active._available_saved(state))}"
    text += f"\n✅ مستعملة: {len(active._used_topics(state))}"
    if active._production_enabled():
        text += f"\n\n🔐 بدء الإنتاج: يتطلب كتابة «{PRODUCTION_CONFIRMATION_TEXT}» بعد اعتماد موضوع"
    else:
        text += "\n\n🔒 إنتاج Telegram: مقفول"
    text += "\n🔒 نشر YouTube: يدوي فقط"
    return text


def _stats_result_keyboard(url: str | None = None) -> list[list[dict[str, str]]]:
    rows: list[list[dict[str, str]]] = []
    if url:
        rows.append([{"text": "▶️ فتح على YouTube", "url": url}])
    rows.append([{"text": "↩️ الإحصائيات", "callback_data": "cmd:stats_menu"}])
    rows.append([{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}])
    return rows


def _live_stats(state: dict[str, Any]) -> dict[str, Any]:
    live = youtube_stats.fetch_live(os.environ.get("YOUTUBE_API_KEY", ""))
    youtube_stats.record_snapshot(state, live)
    return live


def _send_stats(kind: str, client, state: dict[str, Any], chat_id) -> None:
    try:
        live = _live_stats(state)
        url: str | None = None
        if kind == "stats_last_long":
            text, url = youtube_stats.render_latest(live, short=False)
        elif kind == "stats_last_short":
            text, url = youtube_stats.render_latest(live, short=True)
        elif kind == "stats_today":
            text = youtube_stats.render_period(live, state, days=1)
        elif kind == "stats_week":
            text = youtube_stats.render_period(live, state, days=7)
        else:
            text = youtube_stats.render_overview(live)
        client.send(chat_id, text, keyboard=_stats_result_keyboard(url))
    except Exception as exc:
        print(f"Telegram YouTube stats failed: {type(exc).__name__}: {exc}")
        client.send(
            chat_id,
            "⚠️ تعذر تحديث إحصائيات YouTube في هذه اللحظة. لم يتأثر البحث أو الإنتاج. جرّب الإحصائيات مرة أخرى.",
            keyboard=[[{"text": "↩️ الإحصائيات", "callback_data": "cmd:stats_menu"}], [{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}]],
        )


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "menu":
        client.send(chat_id, _menu_text(), keyboard=_main_keyboard())
        return
    if kind == "search_menu":
        client.send(chat_id, _search_menu_text(), keyboard=_search_keyboard())
        return
    if kind == "library_menu":
        client.send(chat_id, _library_menu_text(), keyboard=_library_keyboard())
        return
    if kind == "stats_menu":
        client.send(chat_id, _stats_menu_text(), keyboard=_stats_keyboard())
        return
    if kind in {"stats_last_long", "stats_last_short", "stats_today", "stats_week", "stats_overview"}:
        _send_stats(kind, client, state, chat_id)
        return
    if kind == "produce_latest":
        # Fail closed for stale inline buttons from pre-migration Telegram messages.
        client.send(chat_id, _stale_start_button_text(), keyboard=_main_keyboard())
        return
    if kind == "confirm_production":
        if not active._production_enabled():
            client.send(chat_id, "🔒 إنتاج Telegram مقفول حاليًا.", keyboard=_main_keyboard())
            return
        # Reuse the already-certified exact-target queue path. The caller reaches
        # this handler only after panel.poll has passed TELEGRAM_ALLOWED_USER_ID
        # and TELEGRAM_CHAT_ID authorization.
        _BASE_HANDLE_COMMAND("produce_latest", client, state, releases, chat_id)
        return
    if kind == "status":
        client.send(chat_id, _status_text(state, releases), keyboard=_main_keyboard())
        return
    _BASE_HANDLE_COMMAND(kind, client, state, releases, chat_id)


def _ensure_persistent_start_surface(state_path: Path) -> None:
    """Install/upgrade the one-button reply surface; never weaken auth or dispatch."""
    state = panel.load_state(state_path)
    if int(state.get(PERSISTENT_SURFACE_STATE_KEY, 0) or 0) >= PERSISTENT_SURFACE_VERSION:
        return
    token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE")
    chat_id = panel._read_secret_file("TELEGRAM_CHAT_ID_FILE")
    allowed_user = panel._read_secret_file("TELEGRAM_ALLOWED_USER_ID_FILE")
    if not token or not chat_id or not allowed_user:
        return
    client = panel.TelegramClient(token)
    try:
        client.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "🏠 لوحة نداء اليقظة جاهزة.\n\n"
                    "سيبقى أسفل المحادثة زر واحد فقط: «🏠 ابدأ».\n"
                    "اضغطه لفتح الأقسام، ثم سيظهر كل مستوى فرعي عند الحاجة فقط.\n"
                    "أضفت 📈 الإحصائيات كقسم مستقل للقراءة السريعة من YouTube.\n\n"
                    f"🔐 Production لا يبدأ من أزرار القائمة؛ بعد اعتماد الموضوع يتطلب كتابة «{PRODUCTION_CONFIRMATION_TEXT}» حرفيًا."
                ),
                "disable_web_page_preview": True,
                "reply_markup": _persistent_reply_markup(),
            },
        )
    except Exception as exc:
        print(f"Telegram persistent start surface install failed: {type(exc).__name__}: {exc}")
        return
    state[PERSISTENT_SURFACE_STATE_KEY] = PERSISTENT_SURFACE_VERSION
    state["last_event_at"] = panel._now()
    panel.save_state(state_path, state)
    print("Telegram persistent start surface installed")


def _refresh_youtube_snapshot(state_path: Path) -> None:
    api_key = (os.environ.get("YOUTUBE_API_KEY") or "").strip()
    if not api_key:
        return
    try:
        state = panel.load_state(state_path)
        live = youtube_stats.fetch_live(api_key)
        youtube_stats.record_snapshot(state, live)
        panel.save_state(state_path, state)
    except Exception as exc:
        # Statistics are operational convenience only; they must never block Telegram control.
        print(f"Telegram YouTube snapshot refresh skipped: {type(exc).__name__}: {exc}")


def _poll(state_path: Path) -> None:
    _ensure_persistent_start_surface(state_path)
    _refresh_youtube_snapshot(state_path)
    _BASE_POLL(state_path)


def install() -> None:
    """Install hierarchical presentation, live stats, and text-confirmation policy."""
    global _INSTALLED
    if _INSTALLED:
        return
    active._main_keyboard = _main_keyboard
    active._menu_text = _menu_text
    active._command_kind = _command_kind
    active._approval_text = _approval_text
    active._handle_command = _handle_command
    active._poll = _poll
    panel._candidate_keyboard = _candidate_keyboard
    panel._candidate_panel_text = _candidate_panel_text
    _INSTALLED = True
