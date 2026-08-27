from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# GitHub Actions invokes this entrypoint as ``python scripts/...``. In that mode
# Python puts ``scripts/`` rather than the repository root on sys.path, so
# absolute ``from scripts ...`` imports would fail before Telegram is polled.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_bot_api_10_3_ui as bot_api_10_3_ui
from scripts import telegram_control_active_ui as ui
from scripts import telegram_control_panel as panel


_REQUIRED_POLL_SECRET_FILES = (
    "TELEGRAM_BOT_TOKEN_FILE",
    "TELEGRAM_CHAT_ID_FILE",
    "TELEGRAM_ALLOWED_USER_ID_FILE",
)


def _require_poll_identity(mode: str) -> None:
    if mode != "poll":
        return
    missing = [name for name in _REQUIRED_POLL_SECRET_FILES if not panel._read_secret_file(name)]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            "Telegram editorial control authorization boundary is incomplete; "
            f"missing or empty: {joined}"
        )


def _light_topic_key(token: str) -> str:
    """Return a conservative Arabic morphology key used only as a second-pass match."""
    value = ui._normalize_title(token)
    if not value or " " in value:
        return value
    if value.startswith("ال") and len(value) > 4:
        value = value[2:]
    if value[:1] in {"و", "ف"} and len(value) > 4:
        value = value[1:]
    # Imperfect verb prefixes are useful for pairs such as نستنزف / استنزاف
    # and نخاف / الخوف. This key is never sufficient alone: the caller requires
    # at least three matching content anchors, which keeps the rule conservative.
    if value[:1] in {"ن", "ي", "ت"} and len(value) > 3:
        value = value[1:]
    for suffix in ("كما", "هما", "كم", "كن", "هم", "هن", "ها", "نا", "ك", "ه", "ي"):
        if value.endswith(suffix) and len(value) - len(suffix) >= 3:
            value = value[: -len(suffix)]
            break
    skeleton = "".join(ch for ch in value if ch not in {"ا", "و", "ي"})
    return skeleton if len(skeleton) >= 2 else value


def _base_same_topic_title(left: str, right: str) -> bool:
    matcher = getattr(ui, "_ORIGINAL_SAME_TOPIC_TITLE", ui._same_topic_title)
    return bool(matcher(left, right))


def _same_topic_across_formats(left: str, right: str) -> bool:
    if _base_same_topic_title(left, right):
        return True
    left_tokens = ui._topic_tokens(left)
    right_tokens = ui._topic_tokens(right)
    if min(len(left_tokens), len(right_tokens)) < 3:
        return False
    left_keys = {_light_topic_key(token) for token in left_tokens if _light_topic_key(token)}
    right_keys = {_light_topic_key(token) for token in right_tokens if _light_topic_key(token)}
    if min(len(left_keys), len(right_keys)) < 3:
        return False
    common = len(left_keys & right_keys)
    containment = common / min(len(left_keys), len(right_keys))
    jaccard = common / len(left_keys | right_keys)
    return common >= 3 and containment >= 0.80 and jaccard >= 0.60


def _is_used_topic_globally(state: dict[str, Any], kind: str, title: str) -> bool:
    del kind  # A completed topic is blocked across long and short research.
    return any(
        _same_topic_across_formats(title, str(item.get("topic") or ""))
        for item in ui._used_topics(state)
    )


def _approve_without_consuming_saved(
    state: dict[str, Any], session: dict[str, Any], index: int, scope: str
) -> dict[str, Any]:
    session_id = str(session.get("session_id") or "").strip()
    active_session = str(state.get(ui.ACTIVE_RESEARCH_SESSION_KEY) or "").strip()
    if not session_id or session_id != active_session:
        raise RuntimeError("This Telegram selection is not from the current research session")
    request = ui.simple._approve(state, session, index, scope)
    # Keep the idea in Saved until Production actually succeeds. The production
    # workflow moves/removes it only after the deterministic delivery Release exists.
    state[ui.PRODUCTION_TARGET_KEY] = {
        "request_id": str(request.get("request_id") or ""),
        "request_sha256": str(request.get("request_sha256") or ""),
        "session_id": session_id,
        "selected_at": str(request.get("approved_at") or panel._now()),
    }
    return request


def _menu_text_global_history() -> str:
    return ui._ORIGINAL_MENU_TEXT().replace(
        "ولا يعود في بحث النوع نفسه.",
        "ولا يعود في أي بحث جديد، حلقة أو شورت.",
    )


def _used_page_global_history(state: dict[str, Any], page: int):
    text, keyboard = ui._ORIGINAL_USED_PAGE(state, page)
    return text.replace(
        "وتمنع إعادة الموضوع في بحث النوع نفسه.",
        "وتمنع إعادة الموضوع في أي بحث جديد، حلقة أو شورت.",
    ), keyboard


def _format_label(kind: str) -> tuple[str, str]:
    if kind == "long":
        return "🎬", "طويل"
    if kind == "short":
        return "⚡", "شورت"
    raise RuntimeError("Unsupported Telegram library format")


def _saved_kind_items(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in ui._available_saved(state) if str(item.get("kind") or "") == kind]


def _used_kind_items(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [item for item in ui._used_topics(state) if str(item.get("kind") or "") == kind]


def _saved_kind_menu(state: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    long_count = len(_saved_kind_items(state, "long"))
    short_count = len(_saved_kind_items(state, "short"))
    text = (
        "📚 المحفوظة\n\n"
        "اختر نوع المواضيع المحفوظة:\n\n"
        f"🎬 طويل — {long_count}\n"
        f"⚡ شورت — {short_count}\n\n"
        "كل نوع له قائمته وصفحاته المستقلة."
    )
    keyboard = [
        [{"text": f"🎬 طويل ({long_count})", "callback_data": "cmd:saved-long"}],
        [{"text": f"⚡ شورت ({short_count})", "callback_data": "cmd:saved-short"}],
        [{"text": "↩️ المواضيع", "callback_data": "cmd:library_menu"}],
    ]
    return text, keyboard


def _used_kind_menu(state: dict[str, Any]) -> tuple[str, list[list[dict[str, str]]]]:
    long_count = len(_used_kind_items(state, "long"))
    short_count = len(_used_kind_items(state, "short"))
    text = (
        "✅ المستعملة\n\n"
        "اختر نوع المواضيع التي اكتمل إنتاجها:\n\n"
        f"🎬 طويل — {long_count}\n"
        f"⚡ شورت — {short_count}\n\n"
        "السجل للقراءة فقط، ويمنع إعادة الموضوع في أي بحث جديد."
    )
    keyboard = [
        [{"text": f"🎬 طويل ({long_count})", "callback_data": "cmd:used-long"}],
        [{"text": f"⚡ شورت ({short_count})", "callback_data": "cmd:used-short"}],
        [{"text": "↩️ المواضيع", "callback_data": "cmd:library_menu"}],
    ]
    return text, keyboard


def _saved_page_by_kind(
    state: dict[str, Any], kind: str, page: int
) -> tuple[str, list[list[dict[str, str]]]]:
    icon, label = _format_label(kind)
    items = _saved_kind_items(state, kind)
    if not items:
        search_callback = "cmd:topic" if kind == "long" else "cmd:short"
        search_label = "🎬 بحث حلقة" if kind == "long" else "⚡ بحث شورت"
        return (
            f"📚 المحفوظة — {icon} {label}\n\nلا توجد مواضيع {label} محفوظة حاليًا.",
            [
                [{"text": search_label, "callback_data": search_callback}],
                [{"text": "↩️ المحفوظة", "callback_data": "cmd:saved"}],
            ],
        )
    pages = max(1, (len(items) + ui.SAVED_PAGE_SIZE - 1) // ui.SAVED_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    start = page * ui.SAVED_PAGE_SIZE
    current = items[start : start + ui.SAVED_PAGE_SIZE]
    lines = [
        f"📚 المحفوظة — {icon} {label}",
        "",
        f"{len(items)} موضوعًا محفوظًا — صفحة {page + 1}/{pages}.",
        "اختيار موضوع هنا لا يبدأ Production؛ ستراه أولًا ثم تؤكده بالطريقة المعتادة.",
    ]
    rows: list[list[dict[str, str]]] = []
    for item in current:
        candidate = item["candidate"]
        title = str(candidate.get("title") or "").strip()
        short_title = title if len(title) <= 42 else title[:39].rstrip() + "…"
        rows.append(
            [{"text": f"{icon} {short_title}", "callback_data": f"cmd:savedpick-{item['archive_id']}"}]
        )
    nav: list[dict[str, str]] = []
    prefix = f"cmd:saved-{kind}-page-"
    if page > 0:
        nav.append({"text": "⬅️ أحدث", "callback_data": f"{prefix}{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "أقدم ➡️", "callback_data": f"{prefix}{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "↩️ المحفوظة", "callback_data": "cmd:saved"}])
    return "\n".join(lines), rows


def _used_page_by_kind(
    state: dict[str, Any], kind: str, page: int
) -> tuple[str, list[list[dict[str, str]]]]:
    icon, label = _format_label(kind)
    items = _used_kind_items(state, kind)
    if not items:
        return (
            f"✅ المستعملة — {icon} {label}\n\nلا توجد مواضيع {label} مكتملة الإنتاج في السجل حتى الآن.",
            [[{"text": "↩️ المستعملة", "callback_data": "cmd:used"}]],
        )
    pages = max(1, (len(items) + ui.USED_PAGE_SIZE - 1) // ui.USED_PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    start = page * ui.USED_PAGE_SIZE
    current = items[start : start + ui.USED_PAGE_SIZE]
    lines = [
        f"✅ المستعملة — {icon} {label}",
        "",
        f"{len(items)} موضوعًا مكتمل الإنتاج — صفحة {page + 1}/{pages}.",
        "هذه القائمة للقراءة فقط وتمنع إعادة الموضوع في أي بحث جديد، حلقة أو شورت.",
        "",
    ]
    for index, item in enumerate(current, start + 1):
        lines.append(f"{index}) {icon} {item.get('topic', '')}")
        date = str(item.get("used_at") or "")[:10]
        if date:
            lines.append(f"   {date}")
    rows: list[list[dict[str, str]]] = []
    nav: list[dict[str, str]] = []
    prefix = f"cmd:used-{kind}-page-"
    if page > 0:
        nav.append({"text": "⬅️ أحدث", "callback_data": f"{prefix}{page - 1}"})
    if page + 1 < pages:
        nav.append({"text": "أقدم ➡️", "callback_data": f"{prefix}{page + 1}"})
    if nav:
        rows.append(nav)
    rows.append([{"text": "↩️ المستعملة", "callback_data": "cmd:used"}])
    return "\n".join(lines), rows


def _library_page_request(value: str, bucket: str) -> tuple[str, int] | None:
    for kind in ("long", "short"):
        base = f"{bucket}-{kind}"
        if value == base:
            return kind, 0
        prefix = f"{base}-page-"
        if value.startswith(prefix):
            try:
                return kind, int(value.removeprefix(prefix))
            except ValueError:
                return kind, 0
    return None


def _handle_command_with_library_split(kind, client, state, releases, chat_id) -> None:
    if kind == "saved":
        text, keyboard = _saved_kind_menu(state)
        client.send(chat_id, text, keyboard=keyboard)
        return
    if kind == "used":
        text, keyboard = _used_kind_menu(state)
        client.send(chat_id, text, keyboard=keyboard)
        return
    if isinstance(kind, str):
        saved_request = _library_page_request(kind, "saved")
        if saved_request is not None:
            saved_kind, page = saved_request
            text, keyboard = _saved_page_by_kind(state, saved_kind, page)
            client.send(chat_id, text, keyboard=keyboard)
            return
        used_request = _library_page_request(kind, "used")
        if used_request is not None:
            used_kind, page = used_request
            text, keyboard = _used_page_by_kind(state, used_kind, page)
            client.send(chat_id, text, keyboard=keyboard)
            return
    handler = getattr(ui, "_ISCO_LIBRARY_SPLIT_BASE_HANDLE", None)
    if handler is None:
        raise RuntimeError("Telegram library split base handler is not installed")
    handler(kind, client, state, releases, chat_id)


def _install_policy() -> None:
    if not hasattr(ui, "_ORIGINAL_MENU_TEXT"):
        ui._ORIGINAL_MENU_TEXT = ui._menu_text
    if not hasattr(ui, "_ORIGINAL_USED_PAGE"):
        ui._ORIGINAL_USED_PAGE = ui._used_page
    if not hasattr(ui, "_ORIGINAL_SAME_TOPIC_TITLE"):
        ui._ORIGINAL_SAME_TOPIC_TITLE = ui._same_topic_title
    ui._same_topic_title = _same_topic_across_formats
    ui._is_used_topic = _is_used_topic_globally
    ui._approve_current = _approve_without_consuming_saved
    ui._menu_text = _menu_text_global_history
    ui._used_page = _used_page_global_history


def _install_library_split() -> None:
    """Split Saved and Used into independent Long and Short libraries."""
    if not hasattr(ui, "_ISCO_LIBRARY_SPLIT_BASE_HANDLE"):
        ui._ISCO_LIBRARY_SPLIT_BASE_HANDLE = ui._handle_command
    ui._handle_command = _handle_command_with_library_split


def _clear_candidate_keyboard(session_id: str, kind: str) -> list[list[dict[str, str]]]:
    from scripts import telegram_persistent_control_ui as persistent_ui

    rows = persistent_ui._candidate_keyboard(session_id, kind)
    badges = ("1️⃣", "2️⃣", "3️⃣")
    for index, badge in enumerate(badges):
        if index >= len(rows) or len(rows[index]) < 2:
            break
        rows[index][0]["text"] = f"✅ {badge} اختر هذه الفكرة"
        rows[index][1]["text"] = f"📋 تفاصيل الفكرة {index + 1}"
    return rows


def _clear_candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    from scripts import telegram_persistent_control_ui as persistent_ui

    base = persistent_ui._candidate_panel_text(kind, candidates)
    return (
        base
        + "\n\nطريقة الاختيار واضحة:\n"
        + "✅ اضغط زر «اختر هذه الفكرة» تحت الرقم الذي تريده.\n"
        + "📋 أو افتح التفاصيل أولًا. لا يبدأ Production بمجرد الاختيار."
    )


def _research_started_text(kind: str) -> str:
    if kind == "topic":
        return (
            "🎬 بدأ بحث الحلقة.\n\n"
            "أبحث الآن عن أفضل الخيارات. عند اكتمال البحث سأعرض لك 3 أفكار مرقمة "
            "1️⃣ 2️⃣ 3️⃣، وتحت كل فكرة زر «اختر هذه الفكرة» وزر «تفاصيل الفكرة».\n\n"
            "ℹ️ هذا بحث فقط؛ لا يبدأ Production."
        )
    return (
        "⚡ بدأ بحث الشورت.\n\n"
        "أبحث الآن عن أفضل الخيارات. عند اكتمال البحث سأعرض لك 3 أفكار مرقمة "
        "1️⃣ 2️⃣ 3️⃣، وتحت كل فكرة زر «اختر هذه الفكرة» وزر «تفاصيل الفكرة».\n\n"
        "ℹ️ هذا بحث فقط؛ لا يبدأ Production."
    )


def _handle_command_with_research_clarity(kind, client, state, releases, chat_id) -> None:
    if kind in {"topic", "short"}:
        client.send(chat_id, _research_started_text(kind))
    handler = getattr(ui, "_ISCO_RESEARCH_CLARITY_BASE_HANDLE", None)
    if handler is None:
        raise RuntimeError("Telegram research clarity base handler is not installed")
    handler(kind, client, state, releases, chat_id)


def _install_choice_clarity() -> None:
    """Keep research start and numbered results actionable and unambiguous."""
    panel._candidate_keyboard = _clear_candidate_keyboard
    panel._candidate_panel_text = _clear_candidate_panel_text
    if not hasattr(ui, "_ISCO_RESEARCH_CLARITY_BASE_HANDLE"):
        ui._ISCO_RESEARCH_CLARITY_BASE_HANDLE = ui._handle_command
    ui._handle_command = _handle_command_with_research_clarity
    # Bot API 10.3 is progressive enhancement only: rich candidate cards,
    # in-message buttons/details and DisabledButton fall back to the tested V2 surface.
    bot_api_10_3_ui.install()


def main() -> None:
    _install_policy()
    # Install the persistent reply-keyboard entry point and text-only production
    # confirmation after global topic-memory policy so both policies compose.
    from scripts import telegram_canonical_status_bridge as canonical_status_bridge
    from scripts import telegram_persistent_control_ui as persistent_ui
    from scripts import telegram_rich_integration as rich_integration

    persistent_ui.install()
    _install_library_split()
    _install_choice_clarity()
    # Route both legacy rich status surfaces through the canonical contract before
    # the final integration layer is installed. This removes runtime interpretation drift.
    canonical_status_bridge.install()
    # Install last so status/delivery rich surfaces wrap the final persistent and
    # research handlers without changing the text-only Production authority gate.
    rich_integration.install()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    _require_poll_identity(mode)
    ui.main()


if __name__ == "__main__":
    main()
