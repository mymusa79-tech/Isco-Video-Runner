from __future__ import annotations

from typing import Any

from scripts import telegram_control_panel as panel


_CALLBACK_CONTEXT_ATTR = "_ISCO_BOT_API_10_3_CALLBACK_QUERY_ID"


class CandidatePanelText(str):
    """Plain-text fallback carrying structured candidates for Bot API 10.3."""

    def __new__(cls, value: str, *, kind: str, candidates: list[dict[str, Any]]):
        obj = super().__new__(cls, value)
        obj.kind = kind
        obj.candidates = candidates
        return obj


def _callback_from_keyboard(
    keyboard: list[list[dict[str, Any]]] | None,
    *,
    exact: str | None = None,
    prefix: str | None = None,
) -> str:
    for row in keyboard or []:
        for button in row:
            value = str(button.get("callback_data") or "")
            if exact is not None and value == exact:
                return value
            if prefix is not None and value.startswith(prefix):
                return value
    return ""


def _candidate_rich_message(
    kind: str,
    candidates: list[dict[str, Any]],
    keyboard: list[list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    if len(candidates) < 3:
        raise RuntimeError("Rich Telegram research UI requires three candidates")
    icon = "🎬" if kind == "long" else "⚡"
    heading = "3 خيارات للحلقة" if kind == "long" else "3 خيارات للشورت"
    badges = ("1️⃣", "2️⃣", "3️⃣")
    pick_prefix = "pickshort:" if kind == "short" else "pick:"
    blocks: list[dict[str, Any]] = [
        {"type": "heading", "size": 2, "text": f"{icon} {heading}"},
        {
            "type": "paragraph",
            "text": "راجع الخيارات ثم اختر واحدة. فتح التفاصيل لا يغيّر أي قرار، والاختيار وحده لا يبدأ Production.",
        },
    ]
    for index, item in enumerate(candidates[:3]):
        title = str(item.get("title") or "").strip()
        if not title:
            raise RuntimeError("Rich Telegram candidate title is missing")
        score = float(item.get("control_score", item.get("opportunity_score", 0.0)) or 0.0) * 10
        why = [str(value).strip() for value in (item.get("why") or []) if str(value).strip()]
        pick = _callback_from_keyboard(keyboard, prefix=pick_prefix)
        expected_suffix = f":{index}"
        for row in keyboard or []:
            for button in row:
                value = str(button.get("callback_data") or "")
                if value.startswith(pick_prefix) and value.endswith(expected_suffix):
                    pick = value
                    break
            if pick.endswith(expected_suffix):
                break
        if not pick or not pick.endswith(expected_suffix):
            raise RuntimeError("Rich Telegram selection callback is missing")
        summary = f"⭐ فرصة: {score:.1f}/10"
        if why:
            summary += " · " + " — ".join(why[:2])
        detail = panel._candidate_detail(item, index)
        blocks.extend(
            [
                {"type": "heading", "size": 3, "text": f"{badges[index]} {title}"},
                {"type": "paragraph", "text": summary},
                {
                    "type": "details",
                    "summary": f"📋 تفاصيل الفكرة {index + 1}",
                    "blocks": [{"type": "paragraph", "text": detail}],
                },
                {
                    "type": "buttons",
                    "align": "right",
                    "buttons": [
                        {
                            "text": f"✅ {badges[index]} اختر هذه الفكرة",
                            "style": "success",
                            "callback_data": pick,
                        }
                    ],
                },
            ]
        )
        if index < 2:
            blocks.append({"type": "divider"})

    refresh = _callback_from_keyboard(keyboard, prefix="refresh:")
    menu = _callback_from_keyboard(keyboard, exact="cmd:menu")
    footer_buttons: list[dict[str, Any]] = []
    if refresh:
        footer_buttons.append({"text": "🔄 3 خيارات أخرى", "style": "primary", "callback_data": refresh})
    if menu:
        footer_buttons.append({"text": "🏠 الرئيسية", "style": "link", "callback_data": menu})
    if footer_buttons:
        blocks.extend([{"type": "divider"}, {"type": "buttons", "align": "right", "buttons": footer_buttons}])
    blocks.append(
        {
            "type": "footer",
            "text": "🔐 Production لا يبدأ من هذه البطاقة. بعد اعتماد الموضوع يبقى حاجز التأكيد النصي مستقلًا.",
        }
    )
    return {"blocks": blocks, "is_rtl": True, "skip_entity_detection": True}


def _research_busy_keyboard() -> list[list[dict[str, Any]]]:
    """Bot API 10.3 disabled button prevents a meaningless duplicate click."""
    return [
        [{"text": "⏳ البحث جارٍ", "disabled": {}}],
        [{"text": "🏠 الرئيسية", "callback_data": "cmd:menu"}],
    ]


def _is_research_start_text(text: str) -> bool:
    return text.startswith("🎬 بدأ بحث الحلقة") or text.startswith("⚡ بدأ بحث الشورت")


def _candidate_panel_text(kind: str, candidates: list[dict[str, Any]]) -> CandidatePanelText:
    base = getattr(panel, "_ISCO_BOT_API_10_3_BASE_PANEL_TEXT", None)
    if base is None:
        raise RuntimeError("Telegram Bot API 10.3 base candidate renderer is not installed")
    return CandidatePanelText(str(base(kind, candidates)), kind=kind, candidates=candidates)


def _remember_callback_query(self, callback_id: str, text: str = "") -> None:
    setattr(self, _CALLBACK_CONTEXT_ATTR, str(callback_id or "").strip())
    base = getattr(type(self), "_ISCO_BOT_API_10_3_BASE_ANSWER_CALLBACK", None)
    if base is None:
        raise RuntimeError("Telegram Bot API 10.3 base callback answer is not installed")
    return base(self, callback_id, text)


def consume_callback_query_id(client) -> str | None:
    value = str(getattr(client, _CALLBACK_CONTEXT_ATTR, "") or "").strip()
    setattr(client, _CALLBACK_CONTEXT_ATTR, "")
    return value or None


def _clear_callback_query_id(client) -> None:
    setattr(client, _CALLBACK_CONTEXT_ATTR, "")


def _send_with_bot_api_10_3(
    self,
    chat_id: int | str,
    text: str,
    *,
    keyboard: list[list[dict[str, Any]]] | None = None,
):
    base_send = getattr(type(self), "_ISCO_BOT_API_10_3_BASE_SEND", None)
    if base_send is None:
        raise RuntimeError("Telegram Bot API 10.3 base sender is not installed")

    try:
        if isinstance(text, CandidatePanelText):
            try:
                return self.call(
                    "sendRichMessage",
                    {
                        "chat_id": chat_id,
                        "rich_message": _candidate_rich_message(text.kind, text.candidates, keyboard),
                    },
                )
            except Exception:
                return base_send(self, chat_id, str(text), keyboard=keyboard)

        if _is_research_start_text(str(text)) and keyboard is None:
            try:
                return base_send(self, chat_id, str(text), keyboard=_research_busy_keyboard())
            except Exception:
                return base_send(self, chat_id, str(text), keyboard=None)

        return base_send(self, chat_id, str(text), keyboard=keyboard)
    finally:
        # Any ordinary callback response consumed the one-update context. This
        # prevents a later text command from accidentally reusing a stale query id.
        _clear_callback_query_id(self)


def install() -> None:
    """Install Bot API 10.3 UX and the read-only Channel OS UI junction."""
    if not hasattr(panel, "_ISCO_BOT_API_10_3_BASE_PANEL_TEXT"):
        panel._ISCO_BOT_API_10_3_BASE_PANEL_TEXT = panel._candidate_panel_text
        panel._candidate_panel_text = _candidate_panel_text
    if not hasattr(panel.TelegramClient, "_ISCO_BOT_API_10_3_BASE_SEND"):
        panel.TelegramClient._ISCO_BOT_API_10_3_BASE_SEND = panel.TelegramClient.send
        panel.TelegramClient.send = _send_with_bot_api_10_3
    if not hasattr(panel.TelegramClient, "_ISCO_BOT_API_10_3_BASE_ANSWER_CALLBACK"):
        panel.TelegramClient._ISCO_BOT_API_10_3_BASE_ANSWER_CALLBACK = panel.TelegramClient.answer_callback
        panel.TelegramClient.answer_callback = _remember_callback_query

    # `_install_choice_clarity()` invokes this installer in both certified ingress
    # modes: webhook replay and the webhook-inactive fallback poll. Installing the
    # Channel OS read-only adapter here keeps those paths behaviorally identical
    # without granting Channel OS any Telegram token, update ownership, or Production
    # authority of its own.
    from scripts import channel_os_telegram_adapter

    channel_os_telegram_adapter.install()
