from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel

START_BUTTON_TEXT = "🎛 ابدأ"
PRODUCTION_CONFIRMATION_TEXT = "تأكيد الإنتاج"
PERSISTENT_SURFACE_VERSION = 1
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
        "input_field_placeholder": "اضغط «🎛 ابدأ» لفتح الخيارات",
    }


def _main_keyboard() -> list[list[dict[str, str]]]:
    """Small numbered inline menu; production dispatch is intentionally absent."""
    return [
        [{"text": "1️⃣ 🔎 بحث حلقة", "callback_data": "cmd:topic"}],
        [{"text": "2️⃣ ⚡ بحث شورت", "callback_data": "cmd:short"}],
        [{"text": "3️⃣ 📚 المحفوظة", "callback_data": "cmd:saved"}],
        [{"text": "4️⃣ ✅ المستعملة", "callback_data": "cmd:used"}],
        [{"text": "5️⃣ 🎁 آخر إنتاج", "callback_data": "cmd:last_delivery"}],
        [{"text": "6️⃣ 🧭 الحالة", "callback_data": "cmd:status"}],
    ]


def _menu_text() -> str:
    production = (
        "🟢 الإنتاج متاح، لكن لا يوجد زر يطلقه مباشرة.\n"
        f"بعد اعتماد الموضوع اكتب حرفيًا: {PRODUCTION_CONFIRMATION_TEXT}"
        if active._production_enabled()
        else "🔒 إنتاج Telegram مقفول حاليًا."
    )
    return (
        "🎛 نداء اليقظة\n\n"
        "اختر رقمًا واحدًا فقط:\n\n"
        "1️⃣ 🔎 بحث حلقة — يبحث ويقيّم 3 أفكار للحلقة الطويلة.\n"
        "2️⃣ ⚡ بحث شورت — يبحث ويقيّم 3 أفكار قصيرة.\n"
        "3️⃣ 📚 المحفوظة — أفكار جيدة لم تُختر بعد.\n"
        "4️⃣ ✅ المستعملة — ما اكتمل إنتاجه ولا يُعاد اقتراحه.\n"
        "5️⃣ 🎁 آخر إنتاج — آخر حزمة جاهزة وروابطها.\n"
        "6️⃣ 🧭 الحالة — حالة البحث والإنتاج الحالية.\n\n"
        f"{production}\n\n"
        "🔒 YouTube: الرفع والنشر والجدولة يدويًا فقط."
    )


def _command_kind(text: str) -> str | None:
    raw = str(text or "").strip()
    if raw == PRODUCTION_CONFIRMATION_TEXT:
        return "confirm_production"
    value = panel._normalize_command(raw)
    mapping = {
        START_BUTTON_TEXT.casefold(): "menu",
        "1": "topic",
        "١": "topic",
        "1️⃣": "topic",
        "1️⃣ 🔎 بحث حلقة": "topic",
        "2": "short",
        "٢": "short",
        "2️⃣": "short",
        "2️⃣ ⚡ بحث شورت": "short",
        "3": "saved",
        "٣": "saved",
        "3️⃣": "saved",
        "3️⃣ 📚 المحفوظة": "saved",
        "4": "used",
        "٤": "used",
        "4️⃣": "used",
        "4️⃣ ✅ المستعملة": "used",
        "5": "last_delivery",
        "٥": "last_delivery",
        "5️⃣": "last_delivery",
        "5️⃣ 🎁 آخر إنتاج": "last_delivery",
        "6": "status",
        "٦": "status",
        "6️⃣": "status",
        "6️⃣ 🧭 الحالة": "status",
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
                {"text": "🔎 تفاصيل", "callback_data": f"detail:{session_id}:{index}"},
            ]
        )
    rows.append([{"text": "🔄 3 خيارات أخرى", "callback_data": f"refresh:{kind}"}])
    rows.append([{"text": "↩️ اللوحة", "callback_data": "cmd:menu"}])
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
    lines.append("👇 اضغط رقم «اختيار» أو افتح 🔎 التفاصيل أولًا.")
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


def _handle_command(kind, client, state, releases, chat_id) -> None:
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
    """Install the reply keyboard once; presentation failure never weakens auth or dispatch."""
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
                    "🎛 لوحة التحكم الجديدة جاهزة.\n\n"
                    "استخدم زر «🎛 ابدأ» الثابت أسفل المحادثة متى احتجت البحث أو مراجعة الحالة.\n"
                    f"بدء الإنتاج نفسه لا يتم بزر؛ بعد الاعتماد يتطلب كتابة «{PRODUCTION_CONFIRMATION_TEXT}» حرفيًا."
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


def _poll(state_path: Path) -> None:
    _ensure_persistent_start_surface(state_path)
    _BASE_POLL(state_path)


def install() -> None:
    """Install presentation/confirmation policy on top of the existing active control plane."""
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
