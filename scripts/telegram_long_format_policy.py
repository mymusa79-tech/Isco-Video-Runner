from __future__ import annotations

from typing import Any


POLICY_ID = "engine_editorial_fit_v1"
_INSTALLED = False


def apply_new_long_format_policy(state: dict[str, Any], request: dict[str, Any], *, panel) -> dict[str, Any]:
    """Mark a newly approved Telegram Long for deterministic Engine format routing.

    Telegram currently asks the user to approve the topic/scope, not to choose the
    outer Film-vs-Story shape. Historically that UI detail was silently materialized
    as ``film`` and therefore bypassed Engine ``auto`` routing entirely. New Long
    approvals now carry the user's real intent: approve the topic and let the
    certified deterministic editorial-fit router resolve Film/Story before the V4
    Approved Brief is bound.

    Stored requests that predate this policy are not rewritten. Shorts remain
    ``moment``. A future UI can preserve an explicit human choice by setting
    ``format_locked_by_user`` before this adapter runs.
    """
    if not isinstance(request, dict) or request.get("kind") != "long":
        return request
    if request.get("format_locked_by_user") is True:
        return request
    if request.get("source") != "telegram_editorial_control_panel":
        return request
    if request.get("approved_by_user") is not True:
        raise RuntimeError("Long format policy requires an explicitly approved Telegram request")

    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("Long format policy requires a request id")

    request.pop("request_sha256", None)
    request["format"] = "auto"
    request["format_selection_policy"] = POLICY_ID
    request["request_sha256"] = panel._canonical_hash(request)

    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram request registry is malformed")
    requests[request_id] = request
    return request


def install(*, simple, panel) -> None:
    """Install inside Active UI's approval seam, before production-target binding."""
    global _INSTALLED
    if _INSTALLED:
        return
    base_approve = simple._approve

    def _approve_with_long_format_policy(state, session, index, scope):
        request = base_approve(state, session, index, scope)
        return apply_new_long_format_policy(state, request, panel=panel)

    simple._approve = _approve_with_long_format_policy
    _INSTALLED = True
