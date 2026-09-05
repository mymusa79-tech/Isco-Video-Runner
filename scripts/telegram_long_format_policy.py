from __future__ import annotations

from typing import Any


POLICY_VERSION = "professional_long_format_router_v1"
_INSTALLED = False
_BLOCKING_DISPATCH_STATUSES = frozenset({"pending_dispatch", "dispatch_reserved", "dispatch_consumed"})


def _policy_document() -> dict[str, Any]:
    return {
        "version": POLICY_VERSION,
        "requested": "auto",
        "resolution_stage": "v4_before_approved_brief_binding",
        "extra_ai_calls": 0,
    }


def _canonicalize_long_research_pack(request: dict[str, Any]) -> None:
    """Collapse the historical Telegram research-pack alias onto one production contract."""
    legacy_present = "approved_research_pack" in request
    canonical_present = "research_pack" in request
    legacy = request.get("approved_research_pack")
    canonical = request.get("research_pack")

    if legacy_present and canonical_present and legacy != canonical:
        raise RuntimeError("Long request contains conflicting research-pack aliases")
    if legacy_present and not canonical_present:
        request["research_pack"] = legacy
    request.pop("approved_research_pack", None)

    # This adapter owns schema normalization, not research sufficiency. The
    # Approved-Brief boundary remains the single owner of the >=2-source gate.
    pack = request.get("research_pack")
    if pack is not None and not isinstance(pack, list):
        raise RuntimeError("Long request research_pack must be a list when present")


def _persist_rehashed_request(state: dict[str, Any], request: dict[str, Any], *, panel) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("Long format policy requires a request id")

    requests = state.get("requests")
    if not isinstance(requests, dict):
        raise RuntimeError("Telegram request registry is malformed")

    request.pop("request_sha256", None)
    request["request_sha256"] = panel._canonical_hash(request)
    requests[request_id] = request

    target = state.get("production_target")
    if isinstance(target, dict) and str(target.get("request_id") or "") == request_id:
        target["request_sha256"] = str(request["request_sha256"])
    return request


def _request_hash_is_valid(request: dict[str, Any], *, panel) -> bool:
    stored = str(request.get("request_sha256") or "").strip()
    if not stored:
        return False
    subject = {key: value for key, value in request.items() if key != "request_sha256"}
    return stored == panel._canonical_hash(subject)


def _has_blocking_dispatch(state: dict[str, Any], request_id: str, request_sha256: str) -> bool:
    queue = state.get("production_queue")
    if not isinstance(queue, list):
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("request_id") or "") == request_id
        and str(item.get("request_sha256") or "") == request_sha256
        and str(item.get("status") or "") in _BLOCKING_DISPATCH_STATUSES
        for item in queue
    )


def _certified_auto_policy(request: dict[str, Any]) -> bool:
    policy = request.get("format_policy")
    return (
        isinstance(policy, dict)
        and str(policy.get("version") or "") == POLICY_VERSION
        and str(policy.get("requested") or "") == "auto"
        and str(policy.get("resolution_stage") or "") == "v4_before_approved_brief_binding"
    )


def apply_new_long_format_policy(state: dict[str, Any], request: dict[str, Any], *, panel) -> dict[str, Any]:
    """Bind every new Telegram Long to the canonical research + format contract.

    Telegram approves the topic/scope, not a Film-vs-Story choice. New Long approvals
    therefore preserve that intent as ``auto`` plus explicit policy provenance. The
    exact production Engine resolves Film/Story later at the V4 seam, before the
    Approved Brief is cryptographically bound.

    The historical UI used ``approved_research_pack`` while Production now owns the
    canonical ``research_pack`` field. Collapse that alias here, at the same immutable
    approval boundary, so a request cannot be correctly routed yet fail one gate later
    because two layers disagree about the same approved evidence.
    """
    if not isinstance(request, dict) or request.get("kind") != "long":
        return request

    _canonicalize_long_research_pack(request)

    if request.get("format_locked_by_user") is True:
        if str(request.get("format") or "").strip().lower() not in {"film", "story"}:
            raise RuntimeError("User-locked Long format must be film or story")
        return _persist_rehashed_request(state, request, panel=panel)

    request["format"] = "auto"
    request["format_policy"] = _policy_document()
    return _persist_rehashed_request(state, request, panel=panel)


def migrate_current_production_target(state: dict[str, Any], *, panel) -> bool:
    """Idempotently repair only the currently approved Telegram production target.

    This is deliberately not a bulk rewrite of historical state. It verifies the old
    immutable hash first, refuses to mutate a request with a live dispatch authority,
    and recognizes only two known legacy drifts: Long ``moment`` and the old
    ``approved_research_pack`` alias. Unknown formats remain fail-closed.
    """
    target = state.get("production_target")
    requests = state.get("requests")
    if not isinstance(target, dict) or not isinstance(requests, dict):
        return False

    request_id = str(target.get("request_id") or "").strip()
    target_sha = str(target.get("request_sha256") or "").strip()
    request = requests.get(request_id) if request_id else None
    if not isinstance(request, dict) or request.get("kind") != "long":
        return False

    stored_sha = str(request.get("request_sha256") or "").strip()
    if not target_sha or stored_sha != target_sha:
        raise RuntimeError("Current Telegram production target is not bound to its stored request hash")
    if not _request_hash_is_valid(request, panel=panel):
        raise RuntimeError("Current Telegram production target request hash is invalid")
    if request.get("approved_by_user") is not True:
        raise RuntimeError("Current Telegram production target lacks explicit user approval")
    if request.get("source") != "telegram_editorial_control_panel":
        raise RuntimeError("Current Telegram production target source is unsupported")
    if request.get("status") != "approved_waiting_production_activation":
        return False
    if request.get("production_dispatch_authorized") is not False:
        raise RuntimeError("Stored Telegram production target unexpectedly owns dispatch authority")
    if _has_blocking_dispatch(state, request_id, stored_sha):
        return False

    fmt = str(request.get("format") or "").strip().lower()
    has_legacy_pack = "approved_research_pack" in request
    needs_format_migration = fmt == "moment"
    if not has_legacy_pack and not needs_format_migration:
        return False

    _canonicalize_long_research_pack(request)

    if needs_format_migration:
        if request.get("format_locked_by_user") is True:
            raise RuntimeError("Cannot migrate a user-locked Long request from moment")
        request["format"] = "auto"
        request["format_policy"] = _policy_document()
    elif fmt == "auto":
        if not _certified_auto_policy(request):
            raise RuntimeError("Stored Long auto format lacks certified policy provenance")
    elif fmt not in {"film", "story"}:
        raise RuntimeError(f"Unsupported stored Long format during migration: {fmt or '<empty>'}")

    _persist_rehashed_request(state, request, panel=panel)
    return True


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
