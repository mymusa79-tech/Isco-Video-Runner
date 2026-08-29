from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.orchestration_telegram_ingress_outbox import (
    ApprovalDecision,
    BoundApproval,
    ReleaseCandidateDigest,
    TelegramControlContractError,
)

STATE_KEY = "release_approval_receipts"
APPROVE_PREFIX = "approve"
REJECT_PREFIX = "reject"
PROJECTION_LIMIT = 32


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise TelegramControlContractError(f"release candidate artifact is missing:{path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_release_asset_set_digest(root: Path, asset_names: tuple[str, ...]) -> str:
    root = Path(root)
    if not asset_names:
        raise TelegramControlContractError("release asset set must not be empty")
    if len(set(asset_names)) != len(asset_names):
        raise TelegramControlContractError("release asset set contains duplicate names")
    records: list[dict[str, object]] = []
    for name in sorted(asset_names):
        if not name or Path(name).name != name:
            raise TelegramControlContractError("release asset names must be root-relative basenames")
        path = root / name
        if not path.is_file():
            raise TelegramControlContractError(f"release asset is missing:{name}")
        records.append({"name": name, "size": path.stat().st_size, "sha256": _sha256_file(path)})
    payload = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_release_candidate(
    *,
    root: Path,
    run_id: str,
    capability_manifest_name: str,
    release_asset_names: tuple[str, ...],
) -> ReleaseCandidateDigest:
    root = Path(root)
    return ReleaseCandidateDigest(
        run_id=run_id,
        final_mp4_sha256=_sha256_file(root / "final.mp4"),
        delivery_manifest_sha256=_sha256_file(root / "delivery-manifest.json"),
        capability_manifest_sha256=_sha256_file(root / capability_manifest_name),
        release_asset_set_digest=canonical_release_asset_set_digest(root, release_asset_names),
    )


def approval_id_for_candidate(candidate: ReleaseCandidateDigest) -> str:
    raw = bytes.fromhex(candidate.digest)
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def candidate_digest_from_approval_id(approval_id: str) -> str:
    value = str(approval_id or "").strip()
    if len(value) != 43:
        raise TelegramControlContractError("approval_id must encode one full SHA-256 digest")
    try:
        raw = base64.urlsafe_b64decode(value + "=")
    except Exception as exc:
        raise TelegramControlContractError("approval_id is not valid base64url") from exc
    if len(raw) != 32:
        raise TelegramControlContractError("approval_id must encode one full SHA-256 digest")
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if canonical != value:
        raise TelegramControlContractError("approval_id is not canonical base64url")
    return raw.hex()


def callback_data_for(candidate: ReleaseCandidateDigest, decision: ApprovalDecision) -> str:
    approval_id = approval_id_for_candidate(candidate)
    prefix = APPROVE_PREFIX if decision is ApprovalDecision.APPROVED else REJECT_PREFIX
    data = f"{prefix}:{approval_id}"
    if len(data.encode("utf-8")) > 64:
        raise TelegramControlContractError("Telegram approval callback exceeds 64-byte limit")
    return data


def parse_callback_data(data: str) -> tuple[str, ApprovalDecision] | None:
    value = str(data or "").strip()
    if ":" not in value:
        return None
    prefix, approval_id = value.split(":", 1)
    if prefix == APPROVE_PREFIX:
        decision = ApprovalDecision.APPROVED
    elif prefix == REJECT_PREFIX:
        decision = ApprovalDecision.REJECTED
    else:
        return None
    candidate_digest_from_approval_id(approval_id)
    return approval_id, decision


def record_webhook_approval(
    state: dict[str, Any],
    *,
    update: Mapping[str, Any],
    allowed_user_id: str,
    allowed_chat_id: str,
    decided_at: str | None = None,
) -> BoundApproval | None:
    callback = update.get("callback_query")
    if not isinstance(callback, Mapping):
        return None
    parsed = parse_callback_data(str(callback.get("data") or ""))
    if parsed is None:
        return None
    approval_id, decision = parsed
    actor = callback.get("from")
    message = callback.get("message")
    actor_id = str(actor.get("id") if isinstance(actor, Mapping) else "")
    chat = message.get("chat") if isinstance(message, Mapping) else None
    chat_id = str(chat.get("id") if isinstance(chat, Mapping) else "")
    if not allowed_user_id or actor_id != str(allowed_user_id):
        raise TelegramControlContractError("release approval actor is not authorized")
    if not allowed_chat_id or chat_id != str(allowed_chat_id):
        raise TelegramControlContractError("release approval chat is not authorized")
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise TelegramControlContractError("release approval update_id is invalid")
    candidate_digest = candidate_digest_from_approval_id(approval_id)
    receipts = state.setdefault(STATE_KEY, {})
    if not isinstance(receipts, dict):
        raise TelegramControlContractError("release approval receipt state is malformed")
    existing = receipts.get(approval_id)
    canonical = {
        "schema_version": 1,
        "approval_id": approval_id,
        "candidate_digest": candidate_digest,
        "decision": decision.value,
        "update_id": update_id,
        "decided_at": decided_at or _utc_now(),
    }
    if existing is not None:
        if not isinstance(existing, dict):
            raise TelegramControlContractError("release approval receipt is malformed")
        same = (
            existing.get("candidate_digest") == candidate_digest
            and existing.get("decision") == decision.value
            and existing.get("update_id") == update_id
        )
        if not same:
            raise TelegramControlContractError("release approval callback conflicts with durable receipt")
        canonical = existing
    else:
        receipts[approval_id] = canonical
    state["last_event_at"] = canonical["decided_at"]
    return BoundApproval(
        approval_id=approval_id,
        actor_id=actor_id,
        update_id=update_id,
        candidate_digest=candidate_digest,
        decision=decision,
        journal_event_ref=f"telegram-release-approval:{approval_id}",
    )


def approval_projection(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    receipts = state.get(STATE_KEY)
    if not isinstance(receipts, Mapping):
        return []
    items: list[dict[str, Any]] = []
    for approval_id, raw in receipts.items():
        if not isinstance(raw, Mapping):
            continue
        try:
            digest = candidate_digest_from_approval_id(str(approval_id))
        except TelegramControlContractError:
            continue
        if str(raw.get("candidate_digest") or "") != digest:
            continue
        decision = str(raw.get("decision") or "")
        if decision not in {ApprovalDecision.APPROVED.value, ApprovalDecision.REJECTED.value}:
            continue
        items.append(
            {
                "approval_id": str(approval_id),
                "candidate_digest": digest,
                "decision": decision,
                "decided_at": str(raw.get("decided_at") or ""),
            }
        )
    items.sort(key=lambda item: (item["decided_at"], item["approval_id"]))
    return items[-PROJECTION_LIMIT:]


def decision_from_projection(
    projection: Mapping[str, Any],
    candidate: ReleaseCandidateDigest,
) -> ApprovalDecision | None:
    approval_id = approval_id_for_candidate(candidate)
    approvals = projection.get("release_approvals")
    if not isinstance(approvals, list):
        return None
    for raw in reversed(approvals):
        if not isinstance(raw, Mapping):
            continue
        if raw.get("approval_id") != approval_id:
            continue
        if raw.get("candidate_digest") != candidate.digest:
            raise TelegramControlContractError("approval projection candidate digest mismatch")
        try:
            return ApprovalDecision(str(raw.get("decision") or ""))
        except ValueError as exc:
            raise TelegramControlContractError("approval projection decision is invalid") from exc
    return None


def effective_decision_after_timeout() -> str:
    return "rejected"
