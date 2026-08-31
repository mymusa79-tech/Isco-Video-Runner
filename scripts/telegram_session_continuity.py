"""Telegram research-session continuity without weakening approval authority.

The control plane historically kept one global ``active_research_session_id`` for
both Long and Short research. That made a perfectly valid Short card become
unselectable as soon as a Long research cycle became active (and vice versa).
A transient failed refresh also cleared the pointer before a replacement session
existed, leaving visible Telegram buttons that could show details but could not be
approved.

This adapter keeps the existing approval gate fail-closed while making its notion
of "current" format-aware:

* a valid successful session survives a pending/failed refresh until a replacement
  session of the same format succeeds;
* the newest stored Long and newest stored Short can each remain selectable;
* an older session of the *same* format is still rejected;
* Production authority is unchanged: choosing a topic only creates the approved
  request/target and never dispatches Production by itself.
"""

from __future__ import annotations

from typing import Any

_STALE_SELECTION_ERROR = "This Telegram selection is not from the current research session"


def _session_id(session: Any) -> str:
    if not isinstance(session, dict):
        return ""
    return str(session.get("session_id") or "").strip()


def _session_kind(session: Any) -> str:
    if not isinstance(session, dict):
        return ""
    value = str(session.get("kind") or "").strip()
    return value if value in {"long", "short"} else ""


def _stored_session(state: dict[str, Any], session_id: str) -> dict[str, Any] | None:
    sessions = state.get("sessions")
    if not isinstance(sessions, dict) or not session_id:
        return None
    value = sessions.get(session_id)
    return value if isinstance(value, dict) else None


def _latest_session_id_for_kind(state: dict[str, Any], kind: str) -> str:
    sessions = state.get("sessions")
    if not isinstance(sessions, dict) or kind not in {"long", "short"}:
        return ""
    ranked: list[tuple[str, str]] = []
    for key, value in sessions.items():
        if not isinstance(value, dict) or _session_kind(value) != kind:
            continue
        session_id = _session_id(value) or str(key or "").strip()
        if not session_id:
            continue
        created_at = str(value.get("created_at") or "").strip()
        ranked.append((created_at, session_id))
    if not ranked:
        return ""
    ranked.sort()
    return ranked[-1][1]


def _is_latest_session_of_kind(state: dict[str, Any], session: dict[str, Any]) -> bool:
    session_id = _session_id(session)
    kind = _session_kind(session)
    stored = _stored_session(state, session_id)
    if stored is None or _session_kind(stored) != kind:
        return False
    return bool(kind and session_id == _latest_session_id_for_kind(state, kind))


def install(*, active: Any, panel: Any) -> None:
    """Install continuity after the final Telegram UI/approval stack is bound."""
    if getattr(panel, "_isco_session_continuity_installed", False):
        return

    active_key = active.ACTIVE_RESEARCH_SESSION_KEY
    production_key = active.PRODUCTION_TARGET_KEY
    original_clear = active._clear_current_selection
    original_approve = panel._approve

    def clear_production_target_preserve_valid_session(state: dict[str, Any]) -> None:
        # New research must invalidate any prior *production target*, but it must
        # not orphan the last successful research card before a replacement exists.
        state.pop(production_key, None)
        current = str(state.get(active_key) or "").strip()
        if current and _stored_session(state, current) is None:
            state.pop(active_key, None)

    def approve_latest_session_of_same_kind(
        state: dict[str, Any], session: dict[str, Any], index: int, scope: str
    ) -> dict[str, Any]:
        try:
            return original_approve(state, session, index, scope)
        except RuntimeError as exc:
            if str(exc) != _STALE_SELECTION_ERROR or not _is_latest_session_of_kind(state, session):
                raise

        # The global pointer may currently refer to the newest session of the
        # *other* format. Temporarily bind it to this latest same-kind session so
        # the existing certified approval function can perform every normal gate.
        # Keep the pointer only on success because Production target validation is
        # intentionally bound to the approved session.
        had_previous = active_key in state
        previous = state.get(active_key)
        state[active_key] = _session_id(session)
        try:
            return original_approve(state, session, index, scope)
        except Exception:
            if had_previous:
                state[active_key] = previous
            else:
                state.pop(active_key, None)
            raise

    setattr(clear_production_target_preserve_valid_session, "_isco_original_clear", original_clear)
    setattr(approve_latest_session_of_same_kind, "_isco_original_approve", original_approve)
    active._clear_current_selection = clear_production_target_preserve_valid_session
    panel._approve = approve_latest_session_of_same_kind
    panel._isco_session_continuity_installed = True
