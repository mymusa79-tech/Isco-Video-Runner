from __future__ import annotations

import sys
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


def main() -> None:
    _install_policy()
    # Layer the persistent entry surface and exact text-confirmation policy only
    # after the global topic-memory policy is installed, so both policies compose.
    from scripts import telegram_persistent_control_ui as persistent_ui

    persistent_ui.install()
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    _require_poll_identity(mode)
    ui.main()


if __name__ == "__main__":
    main()