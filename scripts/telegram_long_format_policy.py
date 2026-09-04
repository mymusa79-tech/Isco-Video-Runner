from __future__ import annotations

from typing import Any


POLICY_VERSION = "professional_long_format_router_v1"
_INSTALLED = False


def apply_new_long_format_policy(state: dict[str, Any], request: dict[str, Any], *, panel) -> dict[str, Any]:
    """Mark a newly approved Telegram Long for deterministic Engine format routing.

    Telegram approves the topic/scope, not a Film-vs-Story choice. New Long approvals
    therefore preserve that intent as ``auto`` plus explicit policy provenance. The
    exact production Engine resolves Film/Story later at the V4 seam, before the
    Approved Brief is cryptographically bound.

    Existing stored requests are never migrated by this adapter, Shorts remain
    ``moment``, and a future explicit human format choice can set
    ``format_locked_by_user`` before this wrapper executes.
    """
    if not isinstance(request, dict) or request.get("kind") != "long":
        return request
    if request.get("format_locked_by_user") is True:
        return request

    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("Long format policy requires a request id")

    request.pop("request_sha256", None)
    request["format"] = "auto"
    request["format_policy"] = {
        "version": POLICY_VERSION,
        "requested": "auto",
        "resolution_stage": "v4_before_approved_brief_binding",
        "extra_ai_calls": 0,
    }
    request["request_sha256"] = panel._canonical_hash(request)

    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram request registry is malformed")
    requests[request_id] = request

    # Active UI binds production_target immediately after approval. Because this
    # policy intentionally runs as the final approval wrapper, keep that pointer on
    # the re-hashed immutable request instead of leaving a stale pre-policy hash.
    target = state.get("production_target")
    if isinstance(target, dict) and str(target.get("request_id") or "") == request_id:
        target["request_sha256"] = str(request["request_sha256"])

    return request


def install(*, panel) -> None:
    """Wrap the final Telegram approval owner after all presentation/state layers."""
    global _INSTALLED
    if _INSTALLED:
        return
    base_approve = panel._approve

    def _approve_with_long_format_policy(state, session, index, scope):
        request = base_approve(state, session, index, scope)
        return apply_new_long_format_policy(state, request, panel=panel)

    panel._approve = _approve_with_long_format_policy
    _INSTALLED = True
