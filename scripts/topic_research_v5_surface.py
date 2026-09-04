"""End-to-end V5 presentation bridge for Topic Research multi-stage ranking.

The ranking policy owns admission/classification/scoring. Creator Control Center V5
owns navigation and Telegram ergonomics. This bridge binds the two *after* V5 is
installed so the final user-visible surface cannot relabel a low-current Evergreen
as "best now", promise three results when only one/two survived admission, or
recreate impossible choice buttons when reopening a partial research session.

No approval, Production, provider, evidence, Quality, or Safety authority lives here.
"""

from __future__ import annotations

from typing import Any

from scripts import topic_research_ranking_policy as ranking

TARGET_OPTIONS = 3


def _ready_count_label(count: int) -> str:
    value = max(0, int(count))
    if value == 1:
        return "خيار واحد"
    if value == 2:
        return "خياران"
    return f"{value} خيارات"


def _search_text() -> str:
    return (
        "🔎 بحث جديد\n\n"
        "اختر النوع. سأبحث عن حتى 3 فرص حية مرتبة، وأفصل بوضوح بين «فرصة الآن» وEvergreen.\n\n"
        "هذا بحث فقط؛ لا يبدأ Production."
    )


def _research_started_text(kind: str) -> str:
    label = "الحلقة" if kind == "topic" else "الشورت"
    return (
        f"🔎 بدأ بحث {label}\n\n"
        "أبحث عن حتى 3 فرص حية جديدة وأقارن توقيت السوق بملاءمة القناة والقوة الإبداعية وثقة التنفيذ.\n"
        "إذا لم توجد فرصة قوية الآن فلن أصف Evergreen منخفض الزخم كأنه ترند.\n"
        "هذه البطاقة نفسها ستتحدث عند إعادة المحاولة أو اكتمال البحث.\n\n"
        "🔐 البحث لا يبدأ Production."
    )


def install(*, core: Any, panel: Any, creator_v5: Any) -> None:
    """Bind truthful ranking presentation after Creator V5 owns navigation."""
    if getattr(creator_v5, "_isco_topic_ranking_surface_installed", False):
        return

    previous_status = creator_v5._operator_status
    previous_handle = creator_v5._handle_command

    def operator_status(state: dict[str, Any], releases):
        text, rows = previous_status(state, releases)
        # V5 historically claimed "3 options ready" whenever a session existed.
        # Topic Research V2 now intentionally allows one/two valid measured results,
        # and ranking admission can remove Explore candidates. Correct only that
        # ready-session projection while preserving all Production/status branches.
        try:
            target = creator_v5.active._current_target(state)
            session = creator_v5._ready_research_session(state) if target is None else None
        except Exception:
            session = None
        if isinstance(session, dict):
            candidates = session.get("candidates")
            count = min(TARGET_OPTIONS, len(candidates)) if isinstance(candidates, list) else 0
            if count > 0:
                text = text.replace(
                    "مكتمل و3 خيارات جاهزة للمراجعة.",
                    f"مكتمل و{_ready_count_label(count)} جاهز للمراجعة.",
                )
        return text, rows

    def handle_command(kind, client, state, releases, chat_id) -> None:
        # Reopen a successful partial session with exactly the buttons that can be
        # selected. Do not fall back to the legacy three-button panel.
        if isinstance(kind, str) and kind.startswith("choices-"):
            session_id = kind.removeprefix("choices-").strip()
            session = panel._session(state, session_id)
            candidates = session.get("candidates") if isinstance(session, dict) else None
            if isinstance(candidates, list) and candidates:
                kind_value = str(session.get("kind") or "long")
                client.send(
                    chat_id,
                    ranking._candidate_panel_text(kind_value, candidates),
                    keyboard=core._candidate_keyboard(
                        session_id,
                        kind_value,
                        min(TARGET_OPTIONS, len(candidates)),
                    ),
                )
                return
        previous_handle(kind, client, state, releases, chat_id)

    setattr(operator_status, "_isco_topic_ranking_base_status", previous_status)
    setattr(handle_command, "_isco_topic_ranking_base_handle", previous_handle)

    # V5 install runs after ranking.install() and otherwise replaces its card.
    # Rebind only presentation/navigation hooks; decision and approval authority
    # stay with Topic Research V2 and the existing Telegram control plane.
    creator_v5._candidate_panel_text = ranking._candidate_panel_text
    creator_v5._search_text = _search_text
    creator_v5._research_started_text = _research_started_text
    creator_v5._operator_status = operator_status
    creator_v5._handle_command = handle_command
    creator_v5.memory_ui._research_started_text = _research_started_text

    core._candidate_panel_text = ranking._candidate_panel_text
    panel._candidate_panel_text = ranking._candidate_panel_text
    panel._handle_command = handle_command

    creator_v5._isco_topic_ranking_surface_installed = True
