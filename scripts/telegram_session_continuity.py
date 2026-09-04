"""Telegram research-session continuity without weakening approval authority.

Research sessions are transient discovery state. An approved Production decision is
durable operator intent and must not be invalidated merely because another research
cycle starts, fails, or switches between Long and Short.

This adapter keeps the approval gate fail-closed while making two independent facts
explicit:

* newest successful Long and Short research sessions remain independently selectable;
* an already-approved request remains the Production target until the operator
  approves a replacement request or the production queue consumes/reconciles it.

The adapter never dispatches Production by itself.
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


def _approved_target(state: dict[str, Any], production_key: str) -> dict[str, str] | None:
    """Validate durable approval by immutable request identity, not UI session pointer."""
    target = state.get(production_key)
    if not isinstance(target, dict):
        return None
    request_id = str(target.get("request_id") or "").strip()
    request_sha256 = str(target.get("request_sha256") or "").strip()
    session_id = str(target.get("session_id") or "").strip()
    if not request_id or not request_sha256:
        return None

    requests = state.get("requests")
    request = requests.get(request_id) if isinstance(requests, dict) else None
    if not isinstance(request, dict):
        return None
    stored_hash = str(request.get("request_sha256") or "").strip()
    if not stored_hash or stored_hash != request_sha256:
        return None

    # Session provenance is retained for audit when available, but it is not
    # authority. Research UI state may legitimately move on after approval.
    return {
        "request_id": request_id,
        "request_sha256": request_sha256,
        "session_id": session_id,
    }


def install(*, active: Any, panel: Any) -> None:
    """Install continuity after the final Telegram UI/approval stack is bound."""
    if getattr(panel, "_isco_session_continuity_installed", False):
        return

    active_key = active.ACTIVE_RESEARCH_SESSION_KEY
    production_key = active.PRODUCTION_TARGET_KEY
    original_clear = active._clear_current_selection
    original_approve = panel._approve
    original_current_target = getattr(active, "_current_target", None)

    def preserve_approved_target_and_valid_session(state: dict[str, Any]) -> None:
        # Starting/refreshing Research must not revoke an already-approved request.
        # Keep the most recent valid research card until a same-kind replacement
        # succeeds; remove only an impossible dangling pointer.
        current = str(state.get(active_key) or "").strip()
        if current and _stored_session(state, current) is None:
            state.pop(active_key, None)

    def current_approved_target(state: dict[str, Any]) -> dict[str, str] | None:
        return _approved_target(state, production_key)

    def approve_latest_session_of_same_kind(
        state: dict[str, Any], session: dict[str, Any], index: int, scope: str
    ) -> dict[str, Any]:
        try:
            return original_approve(state, session, index, scope)
        except RuntimeError as exc:
            if str(exc) != _STALE_SELECTION_ERROR or not _is_latest_session_of_kind(state, session):
                raise

        # The global research pointer may currently refer to the newest session of
        # the other format. Temporarily bind it so every existing approval gate runs.
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

    setattr(preserve_approved_target_and_valid_session, "_isco_original_clear", original_clear)
    setattr(approve_latest_session_of_same_kind, "_isco_original_approve", original_approve)
    if original_current_target is not None:
        setattr(current_approved_target, "_isco_original_current_target", original_current_target)

    active._clear_current_selection = preserve_approved_target_and_valid_session
    if original_current_target is not None:
        active._current_target = current_approved_target
    panel._approve = approve_latest_session_of_same_kind
    panel._isco_session_continuity_installed = True
