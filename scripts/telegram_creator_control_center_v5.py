from __future__ import annotations

from typing import Any, Iterable

from scripts import telegram_control_active_ui as active
from scripts import telegram_control_panel as panel
from scripts import telegram_persistent_control_ui as persistent_ui
from scripts import telegram_research_status as research_status
from scripts import telegram_topic_memory_ui as memory_ui

CONFIRM_TEXT = persistent_ui.PRODUCTION_CONFIRMATION_TEXT
_ACTIVE_PRODUCTION_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved", "dispatch_consumed"})


def _clip(value: object, limit: int = 72) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _main_keyboard() -> list[list[dict[str, Any]]]:
    return [
        [
            {"text": "🔎 البحث", "callback_data": "cmd:search_menu"},
            {"text": "📚 المواضيع", "callback_data": "cmd:library_menu"},
        ],
        [
            {"text": "🎁 آخر إنتاج", "callback_data": "cmd:last_delivery"},
            {"text": "📈 الإحصائيات", "callback_data": "cmd:stats_menu"},
        ],
        [{"text": "🧭 الحالة", "callback_data": "cmd:status"}],
    ]


def _menu_text() -> str:
    return (
        "🏠 نداء اليقظة — مركز التحكم\n\n"
        "اختر ما تريد إنجازه الآن:\n"
        "🔎 بحث — فرص جديدة للحلقة أو الشورت.\n"
        "📚 المواضيع — المحفوظة والمستعملة.\n"
        "🎁 آخر إنتاج — أحدث حزم التسليم.\n"
        "📈 الإحصائيات — أداء القناة وأحدث المحتوى.\n"
        "🧭 الحالة — ماذا يحدث الآن وهل يلزمك إجراء.\n\n"
        "🔐 اعتماد الفكرة لا يبدأ Production؛ التشغيل يحتاج تأكيدًا نصيًا صريحًا."
    )


def _search_keyboard() -> list[list[dict[str, Any]]]:
    return [
        [
            {"text": "🎬 حلقة", "callback_data": "cmd:topic"},
            {"text": "⚡ شورت", "callback_data": "cmd:short"},
        ],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _search_text() -> str:
    return (
        "🔎 بحث جديد\n\n"
        "اختر النوع. سأبحث عن 3 فرص حية مرتبة وأعرض سبب قوة كل واحدة.\n\n"
        "هذا بحث فقط؛ لا يبدأ Production."
    )


def _library_overview(state: dict[str, Any]) -> tuple[str, list[list[dict[str, Any]]]]:
    saved = active._available_saved(state)
    used = active._used_topics(state)
    saved_long = sum(1 for item in saved if str(item.get("kind") or "") == "long")
    saved_short = sum(1 for item in saved if str(item.get("kind") or "") == "short")
    used_long = sum(1 for item in used if str(item.get("kind") or "") == "long")
    used_short = sum(1 for item in used if str(item.get("kind") or "") == "short")
    text = (
        "📚 مكتبة المواضيع\n\n"
        f"📥 محفوظة: {len(saved)}  ·  🎬 {saved_long} حلقات  ·  ⚡ {saved_short} Shorts\n"
        f"✅ مستعملة: {len(used)}  ·  🎬 {used_long} حلقات  ·  ⚡ {used_short} Shorts\n\n"
        "افتح القسم الذي تحتاجه؛ كل نوع يبقى في قائمة مستقلة."
    )
    rows = [
        [
            {"text": f"📥 المحفوظة ({len(saved)})", "callback_data": "cmd:saved"},
            {"text": f"✅ المستعملة ({len(used)})", "callback_data": "cmd:used"},
        ],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]
    return text, rows


def _stats_keyboard() -> list[list[dict[str, Any]]]:
    return [
        [{"text": "🌐 نظرة عامة", "callback_data": "cmd:stats_overview"}],
        [
            {"text": "🎬 آخر فيديو", "callback_data": "cmd:stats_last_long"},
            {"text": "⚡ آخر Short", "callback_data": "cmd:stats_last_short"},
        ],
        [
            {"text": "🗓️ اليوم", "callback_data": "cmd:stats_today"},
            {"text": "📅 7 أيام", "callback_data": "cmd:stats_week"},
        ],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _stats_text() -> str:
    return (
        "📈 أداء القناة\n\n"
        "ابدأ بالنظرة العامة، أو افتح أحدث فيديو/Short، أو حركة اليوم والأسبوع.\n"
        "CTR والاحتفاظ ومدة المشاهدة التفصيلية تبقى في YouTube Studio."
    )


def _release_rows(release: dict[str, Any] | None, *, short: bool) -> list[dict[str, Any]]:
    if not isinstance(release, dict):
        return []
    url = str(release.get("html_url") or "").strip()
    if not url:
        return []
    label = "⚡ حزمة الشورت" if short else "🎬 حزمة الحلقة"
    return [{"text": label, "url": url}]


def _release_name(release: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(release, dict):
        return fallback
    name = str(release.get("name") or "").strip()
    if name and name != str(release.get("tag_name") or "").strip():
        return _clip(name, 88)
    return fallback


def _release_date(release: dict[str, Any] | None) -> str:
    if not isinstance(release, dict):
        return ""
    return str(release.get("published_at") or release.get("created_at") or "")[:10]


def _production_releases(releases) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if releases is None:
        return None, None
    try:
        items = releases.releases()
    except Exception:
        return None, None
    long_release = next(
        (item for item in items if isinstance(item, dict) and not item.get("draft") and str(item.get("tag_name") or "").startswith("video-")),
        None,
    )
    short_release = next(
        (item for item in items if isinstance(item, dict) and not item.get("draft") and str(item.get("tag_name") or "").startswith("short-")),
        None,
    )
    return long_release, short_release


def _last_delivery(state: dict[str, Any], releases) -> tuple[str, list[list[dict[str, Any]]]]:
    del state
    long_release, short_release = _production_releases(releases)
    lines = ["🎁 آخر إنتاج", ""]
    if long_release:
        lines.extend([
            "🎬 آخر حلقة",
            _release_name(long_release, "حزمة الحلقة الأخيرة"),
            f"✅ جاهزة{(' · ' + _release_date(long_release)) if _release_date(long_release) else ''}",
        ])
    else:
        lines.extend(["🎬 آخر حلقة", "لا توجد حزمة حلقة منشورة بعد."])
    lines.append("")
    if short_release:
        lines.extend([
            "⚡ آخر Short",
            _release_name(short_release, "حزمة الشورت الأخيرة"),
            f"✅ جاهزة{(' · ' + _release_date(short_release)) if _release_date(short_release) else ''}",
        ])
    else:
        lines.extend(["⚡ آخر Short", "لا توجد حزمة Short منشورة بعد."])
    lines.extend(["", "📦 افتح الحزمة فقط عندما تحتاج الملفات أو خيارات النشر."])
    rows: list[list[dict[str, Any]]] = []
    long_buttons = _release_rows(long_release, short=False)
    short_buttons = _release_rows(short_release, short=True)
    if long_buttons:
        rows.append(long_buttons)
    if short_buttons:
        rows.append(short_buttons)
    if isinstance(long_release, dict):
        tag = str(long_release.get("tag_name") or "")
        if tag and len(tag) <= 35:
            rows.append([{"text": "🅰️ عناوين وصور A/B/C", "callback_data": f"pack:{tag}"}])
    rows.append([{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}])
    return "\n".join(lines), rows


def _latest_active_production(state: dict[str, Any]) -> dict[str, Any] | None:
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        return None
    live = [
        item
        for item in queue
        if isinstance(item, dict) and str(item.get("status") or "") in _ACTIVE_PRODUCTION_STATUSES
    ]
    if not live:
        return None
    return max(
        live,
        key=lambda item: (
            str(item.get("consumed_at") or item.get("reserved_at") or item.get("requested_at") or ""),
            int(item.get("attempt", 0) or 0),
        ),
    )


def _ready_research_session(state: dict[str, Any]) -> dict[str, Any] | None:
    session_id = str(state.get(active.ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not session_id:
        return None
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return None
    session = sessions.get(session_id)
    if not isinstance(session, dict):
        return None
    candidates = session.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    return session


def _operator_status(state: dict[str, Any], releases) -> tuple[str, list[list[dict[str, Any]]]]:
    pending = research_status.pending_research(state)
    target = active._current_target(state)
    active_production = _latest_active_production(state)
    ready_session = _ready_research_session(state) if target is None else None
    extra_rows: list[list[dict[str, Any]]] = []

    if pending is not None:
        kind = "الحلقة" if str(pending.get("kind") or "") == "long" else "الشورت"
        attempts = int(pending.get("attempts", 0) or 0)
        now = f"🔎 بحث {kind} قيد التنفيذ أو الانتظار."
        if attempts:
            now += f"\nالمحاولة المنفذة: {attempts}/3."
        action = "لا شيء الآن — نفس بطاقة البحث ستتحدث عند النجاح أو إعادة المحاولة."
    elif active_production is not None:
        status = str(active_production.get("status") or "")
        if status == "pending_dispatch":
            now = "🚀 تم تأكيد Production وهو ينتظر الحجز داخل مسار V4 المحمي."
        elif status == "dispatch_reserved":
            now = "🔐 حُجز تفويض Production لمرة واحدة ويجري تسليمه إلى V4."
        else:
            now = "🎬 Production يعمل الآن داخل V4 الموحد."
            run_id = str(active_production.get("workflow_run_id") or "").strip()
            if run_id:
                now += f"\nRun ID: {run_id}"
        action = "لا شيء الآن — لا تكرر «تأكيد الإنتاج»."
    elif target is not None and active._production_enabled():
        request = state.get("requests", {}).get(target["request_id"], {}) if isinstance(state.get("requests"), dict) else {}
        topic = _clip(request.get("approved_topic"), 90)
        now = "✅ لديك موضوع معتمد ينتظر قرار التشغيل." + (f"\n🎯 {topic}" if topic else "")
        action = f"إذا كان القرار نهائيًا، أرسل حرفيًا: {CONFIRM_TEXT}"
    elif ready_session is not None:
        session_id = str(ready_session.get("session_id") or "").strip()
        kind = "الحلقة" if str(ready_session.get("kind") or "") == "long" else "الشورت"
        now = f"✅ بحث {kind} مكتمل و3 خيارات جاهزة للمراجعة."
        action = "افتح الخيارات واختر ما تريد؛ الاختيار لا يبدأ Production."
        if session_id:
            extra_rows.append([{"text": "📋 فتح الخيارات الجاهزة", "callback_data": f"cmd:choices-{session_id}"}])
    else:
        last_research = state.get("last_research_result")
        if isinstance(last_research, dict) and last_research.get("status") == "failed":
            kind = "الحلقة" if str(last_research.get("kind") or "") == "long" else "الشورت"
            now = f"⚠️ آخر بحث {kind} لم يكتمل بعد استنفاد المحاولات."
            action = "يمكنك بدء بحث جديد عندما تريد."
        else:
            now = "🟢 لا توجد مهمة معلقة تحتاج تدخلك الآن."
            action = "لا يوجد إجراء مطلوب."

    long_release, short_release = _production_releases(releases)
    lines = [
        "🧭 الحالة — ماذا يحدث الآن؟",
        "",
        "الآن",
        now,
        "",
        "مطلوب منك",
        action,
        "",
        "آخر نتائج جاهزة",
        f"🎬 {_release_name(long_release, 'لا توجد حلقة جاهزة')}",
        f"⚡ {_release_name(short_release, 'لا يوجد Short جاهز')}",
    ]
    rows = [
        *extra_rows,
        [{"text": "🔄 تحديث الحالة", "callback_data": "cmd:status"}],
        [{"text": "📋 تفاصيل النظام", "callback_data": "cmd:system_status"}],
        [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
    ]
    return "\n".join(lines), rows


def _system_status(state: dict[str, Any], releases) -> tuple[str, list[list[dict[str, Any]]]]:
    renderer = getattr(panel, "_ISCO_V5_BASE_STATUS_TEXT", None)
    if callable(renderer):
        try:
            text = renderer(state, releases)
        except Exception:
            text = "📋 تفاصيل النظام\n\nتعذر تكوين التفاصيل التقنية الآن."
    else:
        text = "📋 تفاصيل النظام\n\nلا توجد تفاصيل تقنية متاحة في هذه الطبقة."
    text += "\n\nهذه شاشة تشخيص فقط؛ لا تغيّر Production أو Quality Gates."
    return text, [
        [{"text": "🔄 تحديث الحالة", "callback_data": "cmd:status"}],
        [{"text": "↩️ الحالة", "callback_data": "cmd:status"}],
        [{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _market_label(item: dict[str, Any]) -> str:
    value = str(item.get("market_class") or "")
    return {
        "rising": "🔥 زخم حديث قوي",
        "hybrid": "⚖️ الآن + مستمر",
        "evergreen": "🌲 Evergreen بقياس حي",
        "explore": "🧭 فرصة استكشاف",
        "unverified": "⚪ توقيت غير متحقق",
    }.get(value, "🧭 فرصة مقاسة")


def _candidate_note(item: dict[str, Any]) -> str:
    timing = float(item.get("trend_score", 0.0) or 0.0)
    evidence = float(item.get("evidence_quality", 0.0) or 0.0)
    if timing < 0.42:
        return "⚠️ الملاحظة: الزخم الحالي محدود؛ القوة الأساسية Evergreen/تحريرية."
    if evidence < 0.60:
        return "⚠️ الملاحظة: خلفية الدليل أضعف نسبيًا من بقية الخيارات."
    return ""


def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    heading = "🎬 فرص الحلقة" if kind == "long" else "⚡ فرص الشورت"
    badges = ("1️⃣", "2️⃣", "3️⃣")
    lines = [heading, "", "3 قرارات مختصرة. افتح التفاصيل فقط إذا احتجت الأرقام والمصادر.", ""]
    for index, item in enumerate(candidates[:3]):
        score = float(item.get("control_score", item.get("opportunity_score", 0.0)) or 0.0) * 10
        fit = float(item.get("channel_fit_score", item.get("audience_fit", 0.0)) or 0.0) * 10
        timing = float(item.get("trend_score", 0.0) or 0.0) * 10
        why = [str(value) for value in (item.get("why") or []) if str(value).strip()]
        title = _clip(item.get("title"), 110)
        best = " · ⭐ الأنسب الآن" if index == 0 else ""
        lines.append(f"{badges[index]} {title}{best}")
        lines.append(f"   ⭐ {score:.1f}/10  ·  🎯 القناة {fit:.1f}  ·  📈 الآن {timing:.1f}")
        lines.append(f"   {_market_label(item)}")
        if why:
            lines.append(f"   💡 القوة: {why[0]}")
        note = _candidate_note(item)
        if note:
            lines.append("   " + note)
        lines.append("")
    lines.append("👇 اختر الفكرة التي تريدها أو افتح تفاصيلها. الاختيار وحده لا يبدأ Production.")
    return "\n".join(lines).strip()


def _research_started_text(kind: str) -> str:
    label = "الحلقة" if kind == "topic" else "الشورت"
    return (
        f"🔎 بدأ بحث {label}\n\n"
        "أبحث عن 3 فرص حية جديدة وأقارن ملاءمة القناة بتوقيت السوق.\n"
        "هذه البطاقة نفسها ستتحدث عند إعادة المحاولة أو اكتمال البحث.\n\n"
        "🔐 البحث لا يبدأ Production."
    )


def _research_waiting_text(kind: str, attempts: int) -> str:
    label = "الحلقة" if kind == "topic" else "الشورت"
    return (
        f"⏳ بحث {label} ما زال قيد التنفيذ\n\n"
        f"المحاولات المنفذة: {max(0, attempts)}/3.\n"
        "لن أنشئ طلبًا أو رسالة بحث مكررة؛ هذه البطاقة هي الحالة الوحيدة للبحث الجاري.\n\n"
        "🔐 لم يبدأ Production."
    )


def _queue_research(kind: str, client, state: dict[str, Any], chat_id) -> None:
    research_kind = "long" if kind == "topic" else "short"
    queued = panel._queue_research(state, research_kind, chat_id)
    action = research_status.pending_research(state, research_kind)
    if action is None:
        raise RuntimeError("Telegram research queue accepted no durable pending action")
    if queued:
        active._clear_current_selection(state)
        text = _research_started_text(kind)
    else:
        text = _research_waiting_text(kind, int(action.get("attempts", 0) or 0))
    research_status.update_message(
        client,
        action,
        text,
        chat_id=chat_id,
        keyboard=[
            [{"text": "🔄 تحديث الحالة", "callback_data": "cmd:status"}],
            [{"text": "↩️ الرئيسية", "callback_data": "cmd:menu"}],
        ],
    )


def _handle_command(kind, client, state, releases, chat_id) -> None:
    if kind == "menu":
        client.send(chat_id, _menu_text(), keyboard=_main_keyboard())
        return
    if kind == "search_menu":
        client.send(chat_id, _search_text(), keyboard=_search_keyboard())
        return
    if kind == "library_menu":
        text, rows = _library_overview(state)
        client.send(chat_id, text, keyboard=rows)
        return
    if kind == "stats_menu":
        client.send(chat_id, _stats_text(), keyboard=_stats_keyboard())
        return
    if kind in {"topic", "short"}:
        _queue_research(kind, client, state, chat_id)
        return
    if kind == "last_delivery":
        text, rows = _last_delivery(state, releases)
        client.send(chat_id, text, keyboard=rows)
        return
    if kind == "status":
        text, rows = _operator_status(state, releases)
        client.send(chat_id, text, keyboard=rows)
        return
    if kind == "system_status":
        text, rows = _system_status(state, releases)
        client.send(chat_id, text, keyboard=rows)
        return
    if isinstance(kind, str) and kind.startswith("choices-"):
        session_id = kind.removeprefix("choices-").strip()
        session = panel._session(state, session_id)
        if not session:
            client.send(chat_id, "انتهت صلاحية هذه الخيارات. اطلب بحثًا جديدًا.", keyboard=_main_keyboard())
            return
        candidates = session.get("candidates") if isinstance(session, dict) else None
        if not isinstance(candidates, list):
            client.send(chat_id, "هذه الخيارات لم تعد صالحة.", keyboard=_main_keyboard())
            return
        kind_value = str(session.get("kind") or "long")
        client.send(chat_id, _candidate_panel_text(kind_value, candidates), keyboard=panel._candidate_keyboard(session_id, kind_value))
        return
    base = getattr(panel, "_ISCO_V5_BASE_HANDLE", None)
    if not callable(base):
        raise RuntimeError("Telegram Creator Control Center V5 base handler is unavailable")
    base(kind, client, state, releases, chat_id)


def _context_from_update(update: dict[str, Any]) -> dict[str, Any] | None:
    callback = update.get("callback_query") if isinstance(update, dict) else None
    if not isinstance(callback, dict):
        return None
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    message_id = message.get("message_id")
    chat_id = chat.get("id")
    if message_id is None or chat_id is None:
        return None
    return {
        "chat_id": str(chat_id),
        "message_id": int(message_id),
        "data": str(callback.get("data") or ""),
    }


class _BoundUpdates(list):
    def __init__(self, values: Iterable[dict[str, Any]], client):
        super().__init__(values)
        self.client = client

    def __iter__(self):
        try:
            for update in super().__iter__():
                self.client._isco_v5_surface_context = _context_from_update(update)
                yield update
        finally:
            self.client._isco_v5_surface_context = None


def bind_update_batch(client, updates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return _BoundUpdates(list(updates), client)


def _approval_copy_rows(rows: list[list[dict[str, Any]]] | None, text: str) -> list[list[dict[str, Any]]] | None:
    if "✅ تم اعتماد القرار" not in text or CONFIRM_TEXT not in text:
        return rows
    current = [list(row) for row in (rows or [])]
    if any("copy_text" in button for row in current for button in row):
        return current
    return [[{"text": "📋 نسخ عبارة التأكيد", "copy_text": {"text": CONFIRM_TEXT}}], *current]


def _detail_back_rows(rows: list[list[dict[str, Any]]] | None, context: dict[str, Any] | None) -> list[list[dict[str, Any]]] | None:
    if not rows or not context:
        return rows
    data = str(context.get("data") or "")
    if not data.startswith("detail:"):
        return rows
    parts = data.split(":")
    if len(parts) != 3 or not parts[1]:
        return rows
    callback = f"cmd:choices-{parts[1]}"
    fixed: list[list[dict[str, Any]]] = []
    for row in rows:
        new_row: list[dict[str, Any]] = []
        for button in row:
            copied = dict(button)
            if copied.get("callback_data") == "cmd:menu" and "الخيارات" in str(copied.get("text") or ""):
                copied["callback_data"] = callback
            new_row.append(copied)
        fixed.append(new_row)
    return fixed


def _install_client_surface() -> None:
    cls = panel.TelegramClient
    if getattr(cls, "_isco_v5_installed", False):
        return
    cls._isco_v5_installed = True
    cls._isco_v5_base_call = cls.call
    cls._isco_v5_base_send = cls.send

    def call(self, method: str, payload: dict[str, Any] | None = None):
        result = cls._isco_v5_base_call(self, method, payload)
        if method == "getUpdates" and isinstance(result, list):
            return bind_update_batch(self, result)
        if method == "editMessageText":
            context = getattr(self, "_isco_v5_surface_context", None)
            if context and payload:
                if str(payload.get("chat_id")) == str(context.get("chat_id")) and int(payload.get("message_id", -1)) == int(context.get("message_id", -2)):
                    self._isco_v5_surface_context = None
        return result

    def send(self, chat_id, text: str, *, keyboard=None):
        context = getattr(self, "_isco_v5_surface_context", None)
        rows = _approval_copy_rows(keyboard, text)
        rows = _detail_back_rows(rows, context)
        if context and str(chat_id) == str(context.get("chat_id")):
            self._isco_v5_surface_context = None
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": int(context["message_id"]),
                "text": text,
                "disable_web_page_preview": True,
            }
            if rows is not None:
                payload["reply_markup"] = {"inline_keyboard": rows}
            try:
                return self.call("editMessageText", payload)
            except Exception as exc:
                if "message is not modified" in str(exc).casefold():
                    return {"message_id": int(context["message_id"])}
        return cls._isco_v5_base_send(self, chat_id, text, keyboard=rows)

    cls.call = call
    cls.send = send


def install(research_module=None) -> None:
    if not hasattr(panel, "_ISCO_V5_BASE_HANDLE"):
        panel._ISCO_V5_BASE_HANDLE = panel._handle_command
    if not hasattr(panel, "_ISCO_V5_BASE_STATUS_TEXT"):
        panel._ISCO_V5_BASE_STATUS_TEXT = panel._status_text
    panel._handle_command = _handle_command
    panel._main_keyboard = _main_keyboard
    panel._menu_text = _menu_text
    panel._candidate_panel_text = _candidate_panel_text
    memory_ui._research_started_text = _research_started_text
    if research_module is not None:
        research_module._candidate_panel_text = _candidate_panel_text
    _install_client_surface()
