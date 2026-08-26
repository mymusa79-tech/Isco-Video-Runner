from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

# GitHub Actions invokes this entrypoint as ``python scripts/...``. In that mode
# Python puts ``scripts/`` rather than the repository root on sys.path, so
# absolute ``from scripts ...`` imports would fail before Telegram is polled.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import telegram_control_active_ui as ui
from scripts import telegram_control_panel as panel


_REQUIRED_POLL_SECRET_FILES = (
    "TELEGRAM_BOT_TOKEN_FILE",
    "TELEGRAM_CHAT_ID_FILE",
    "TELEGRAM_ALLOWED_USER_ID_FILE",
)
_RETRY_AFTER_RE = re.compile(r"retry\s+in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
MAX_TRANSIENT_QUOTA_RETRIES = 2
MAX_TRANSIENT_QUOTA_WAIT_SECONDS = 70.0
_RESEARCH_CONTEXT: dict[str, str] | None = None
_TERMINAL_QUOTA_MESSAGE: str | None = None
_CHOICE_UX_INSTALLED = False


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
    del kind
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


def _clear_candidate_keyboard(session_id: str, kind: str) -> list[list[dict[str, str]]]:
    from scripts import telegram_persistent_control_ui as persistent_ui

    rows = persistent_ui._candidate_keyboard(session_id, kind)
    badges = ("1️⃣", "2️⃣", "3️⃣")
    for index, badge in enumerate(badges):
        if index >= len(rows) or len(rows[index]) < 2:
            break
        rows[index][0]["text"] = f"✅ {badge} اختر هذه الفكرة"
        rows[index][0]["style"] = "success"
        rows[index][1]["text"] = f"📋 تفاصيل الفكرة {index + 1}"
    return rows


def _clear_candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> str:
    from scripts import telegram_persistent_control_ui as persistent_ui

    base = persistent_ui._candidate_panel_text(kind, candidates)
    return (
        base
        + "\n\nطريقة الاختيار:\n"
        + "✅ اختر الفكرة التي تريدها مباشرة.\n"
        + "📋 أو افتح تفاصيلها ثم ارجع لنفس الخيارات.\n"
        + "🔐 الاختيار وحده لا يبدأ Production."
    )


def _retry_after_seconds(exc: BaseException) -> float | None:
    match = _RETRY_AFTER_RE.search(str(exc))
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if 0.0 < value <= MAX_TRANSIENT_QUOTA_WAIT_SECONDS else None


def _is_quota_error(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in ("429", "resource_exhausted", "quota exceeded", "rate limit"))


def _notify_research(text: str) -> None:
    context = _RESEARCH_CONTEXT or {}
    chat_id = context.get("chat_id", "")
    token = context.get("token", "")
    if not chat_id or not token:
        return
    try:
        panel.TelegramClient(token).send(
            chat_id,
            text,
            keyboard=[[{"text": "🧭 عرض الحالة", "callback_data": "cmd:status"}]],
        )
    except Exception as exc:
        print(f"Telegram quota notice failed: {type(exc).__name__}")


def _resilient_english_research_queries(gemini: str, candidates: list[dict[str, Any]], model: str) -> dict[int, str]:
    global _TERMINAL_QUOTA_MESSAGE
    original = getattr(ui.simple, "_ORIGINAL_ENGLISH_RESEARCH_QUERIES", None)
    if original is None:
        raise RuntimeError("Original scholarly query builder is unavailable")

    for retry_index in range(MAX_TRANSIENT_QUOTA_RETRIES + 1):
        try:
            return original(gemini, candidates, model)
        except Exception as exc:
            if not _is_quota_error(exc):
                raise
            delay = _retry_after_seconds(exc)
            if delay is None:
                _TERMINAL_QUOTA_MESSAGE = (
                    "⏸️ توقف البحث لأن حصة Gemini المجانية غير متاحة الآن.\n\n"
                    "لم أستخدم أي مسار مدفوع ولم يبدأ أي Production. "
                    "أعد البحث لاحقًا عندما تتجدد الحصة المجانية."
                )
                raise
            if retry_index >= MAX_TRANSIENT_QUOTA_RETRIES:
                _TERMINAL_QUOTA_MESSAGE = (
                    "⏸️ ما زال Gemini المجاني عند الحد المؤقت بعد المحاولات المحدودة.\n\n"
                    "أوقفت المحاولة بدل استهلاك طلبات إضافية أو استخدام مسار مدفوع. "
                    "يمكنك إعادة البحث من اللوحة لاحقًا."
                )
                raise
            wait_seconds = min(MAX_TRANSIENT_QUOTA_WAIT_SECONDS, delay + 1.5)
            _notify_research(
                "⏳ Gemini المجاني وصل حد الطلبات المؤقت.\n\n"
                f"سأعيد محاولة البحث تلقائيًا بعد نحو {int(round(wait_seconds))} ثانية. "
                "لا تحتاج إلى الضغط مرة أخرى، ولم يبدأ أي Production."
            )
            print(f"Transient Gemini free-tier quota: bounded retry in {wait_seconds:.1f}s")
            time.sleep(wait_seconds)
    raise RuntimeError("Unreachable quota retry state")


def _rewrite_contextual_keyboard(keyboard: list[list[dict[str, str]]] | None) -> list[list[dict[str, str]]] | None:
    if not keyboard:
        return keyboard
    rows = [[dict(button) for button in row] for row in keyboard]
    session_id = ""
    for row in rows:
        for button in row:
            callback = str(button.get("callback_data") or "")
            parts = callback.split(":")
            if parts and parts[0] in {"pick", "pickshort", "scope"} and len(parts) >= 2:
                session_id = parts[1]
                break
        if session_id:
            break
    if not session_id:
        return rows
    for row in rows:
        for button in row:
            if button.get("text") in {"↩️ الخيارات", "↩️ اللوحة"} and button.get("callback_data") == "cmd:menu":
                button["text"] = "↩️ نفس الخيارات"
                button["callback_data"] = f"cmd:choices-{session_id}"
    return rows


def _install_contextual_send() -> None:
    if hasattr(panel.TelegramClient, "_isco_original_send"):
        return
    original_send = panel.TelegramClient.send
    panel.TelegramClient._isco_original_send = original_send

    def send_with_context(self, chat_id, text, *, keyboard=None):
        global _TERMINAL_QUOTA_MESSAGE
        if _TERMINAL_QUOTA_MESSAGE and str(text).startswith("تعذر البحث"):
            kind = str((_RESEARCH_CONTEXT or {}).get("kind") or "long")
            label = "الحلقة" if kind == "long" else "الشورت"
            text = _TERMINAL_QUOTA_MESSAGE
            keyboard = [
                [{"text": f"🔄 إعادة بحث {label}", "callback_data": f"refresh:{kind}", "style": "primary"}],
                [{"text": "🧭 الحالة", "callback_data": "cmd:status"}],
                [{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}],
            ]
        return original_send(self, chat_id, text, keyboard=_rewrite_contextual_keyboard(keyboard))

    panel.TelegramClient.send = send_with_context


def _choices_handler(kind, client, state, releases, chat_id, original_handler) -> None:
    if not isinstance(kind, str) or not kind.startswith("choices-"):
        original_handler(kind, client, state, releases, chat_id)
        return
    session_id = kind.removeprefix("choices-").strip()
    session = state.get("sessions", {}).get(session_id)
    if not isinstance(session, dict):
        client.send(
            chat_id,
            "⌛ انتهت صلاحية هذه الخيارات. ابدأ بحثًا جديدًا للحصول على 3 أفكار محدثة.",
            keyboard=[[{"text": "🔎 بحث جديد", "callback_data": "cmd:search_menu", "style": "primary"}]],
        )
        return
    candidates = session.get("candidates")
    kind_value = str(session.get("kind") or "long")
    if not isinstance(candidates, list) or len(candidates) < 3:
        client.send(chat_id, "⌛ هذه الجلسة لم تعد صالحة للاختيار.", keyboard=panel._main_keyboard())
        return
    client.send(
        chat_id,
        panel._candidate_panel_text(kind_value, candidates[:3]),
        keyboard=panel._candidate_keyboard(session_id, kind_value),
    )


def _install_choice_clarity() -> None:
    global _CHOICE_UX_INSTALLED
    panel._candidate_keyboard = _clear_candidate_keyboard
    panel._candidate_panel_text = _clear_candidate_panel_text
    _install_contextual_send()
    if _CHOICE_UX_INSTALLED:
        return
    original_handler = panel._handle_command

    def handle_with_choices(kind, client, state, releases, chat_id):
        return _choices_handler(kind, client, state, releases, chat_id, original_handler)

    panel._handle_command = handle_with_choices
    _CHOICE_UX_INSTALLED = True


def _install_research_reliability() -> None:
    if not hasattr(ui.simple, "_ORIGINAL_ENGLISH_RESEARCH_QUERIES"):
        ui.simple._ORIGINAL_ENGLISH_RESEARCH_QUERIES = ui.simple._english_research_queries
    ui.simple._english_research_queries = _resilient_english_research_queries
    if hasattr(ui, "_ORIGINAL_RESEARCH_CURRENT_FOR_QUOTA"):
        return
    ui._ORIGINAL_RESEARCH_CURRENT_FOR_QUOTA = ui._research_current

    def research_with_context(state_path: Path) -> None:
        global _RESEARCH_CONTEXT, _TERMINAL_QUOTA_MESSAGE
        state = panel.load_state(state_path)
        pending = next(
            (item for item in state.get("pending_actions", []) if isinstance(item, dict) and item.get("status") == "pending"),
            None,
        )
        token = panel._read_secret_file("TELEGRAM_BOT_TOKEN_FILE")
        action_id = str((pending or {}).get("action_id") or "")
        _RESEARCH_CONTEXT = {
            "chat_id": str((pending or {}).get("chat_id") or ""),
            "kind": str((pending or {}).get("kind") or "long"),
            "token": token,
        }
        _TERMINAL_QUOTA_MESSAGE = None
        try:
            ui._ORIGINAL_RESEARCH_CURRENT_FOR_QUOTA(state_path)
        except Exception as exc:
            if not (_TERMINAL_QUOTA_MESSAGE and _is_quota_error(exc)):
                raise
            # Expected Free-tier exhaustion is an operational hold, not a broken
            # workflow. Remove the stale pending action so the next explicit search
            # can start cleanly; never dispatch Production from this path.
            state = panel.load_state(state_path)
            state["pending_actions"] = [
                item for item in state.get("pending_actions", [])
                if not (isinstance(item, dict) and str(item.get("action_id") or "") == action_id)
            ]
            state["last_event_at"] = panel._now()
            panel.save_state(state_path, state)
            print("Telegram research stopped cleanly at Gemini Free quota boundary")
        finally:
            _RESEARCH_CONTEXT = None
            _TERMINAL_QUOTA_MESSAGE = None

    ui._research_current = research_with_context


def main() -> None:
    _install_policy()
    from scripts import telegram_persistent_control_ui as persistent_ui

    persistent_ui.install()
    _install_choice_clarity()
    _install_research_reliability()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    _require_poll_identity(mode)
    ui.main()


if __name__ == "__main__":
    main()
