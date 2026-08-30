from __future__ import annotations

"""Telegram control-plane contracts for Production Orchestration L6.

This module owns contract semantics only. It deliberately does not perform HTTP,
GitHub, Telegram, or filesystem I/O. Runtime adapters may persist these immutable
records, but Telegram updates must have one webhook ingress owner per bot token,
and outbound sends must pass through the reconciliation-aware outbox state machine.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Mapping

from scripts.orchestration_journal import CanonicalRunState, project_telegram_status

SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TelegramControlContractError(ValueError):
    pass


class IngressMode(str, Enum):
    WEBHOOK = "WEBHOOK"


class IngressDisposition(str, Enum):
    ACCEPTED = "ACCEPTED"
    DUPLICATE = "DUPLICATE"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    SENT = "SENT"
    FAILED = "FAILED"


class ApprovalDecision(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


def _require_text(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise TelegramControlContractError(f"{name} must be a non-empty string")
    return value


def _require_sha256(name: str, value: str) -> str:
    value = str(value or "").strip().lower()
    if value.startswith("sha256:"):
        value = value.split(":", 1)[1]
    if not _SHA256_RE.fullmatch(value):
        raise TelegramControlContractError(f"{name} must be a SHA-256 digest")
    return value


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class IngressOwnerDeclaration:
    bot_token_hash: str
    owner_id: str
    mode: IngressMode = IngressMode.WEBHOOK

    def __post_init__(self) -> None:
        object.__setattr__(self, "bot_token_hash", _require_sha256("bot_token_hash", self.bot_token_hash))
        object.__setattr__(self, "owner_id", _require_text("owner_id", self.owner_id))
        if self.mode is not IngressMode.WEBHOOK:
            raise TelegramControlContractError("Telegram ingress must be webhook-owned in L6")


def assert_single_ingress_owner(*declarations: IngressOwnerDeclaration) -> None:
    if not declarations:
        raise TelegramControlContractError("at least one Telegram ingress owner is required")
    owners: dict[str, str] = {}
    for declaration in declarations:
        existing = owners.get(declaration.bot_token_hash)
        if existing is not None and existing != declaration.owner_id:
            raise TelegramControlContractError(
                f"multiple ingress owners for one bot token:{existing},{declaration.owner_id}"
            )
        owners[declaration.bot_token_hash] = declaration.owner_id


@dataclass(frozen=True, slots=True)
class TelegramIngressCheckpoint:
    schema_version: int
    bot_token_hash: str
    owner_id: str
    last_update_id: int = -1
    seen_update_hashes: tuple[tuple[int, str], ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise TelegramControlContractError("unsupported Telegram ingress schema version")
        object.__setattr__(self, "bot_token_hash", _require_sha256("bot_token_hash", self.bot_token_hash))
        object.__setattr__(self, "owner_id", _require_text("owner_id", self.owner_id))
        if self.last_update_id < -1:
            raise TelegramControlContractError("last_update_id must be >= -1")
        previous = -1
        for update_id, digest in self.seen_update_hashes:
            if update_id < 0 or update_id <= previous:
                raise TelegramControlContractError("seen update ids must be strictly increasing")
            _require_sha256("seen_update_hash", digest)
            previous = update_id
        if self.seen_update_hashes and self.seen_update_hashes[-1][0] > self.last_update_id:
            raise TelegramControlContractError("seen update cannot exceed last_update_id")

    @property
    def next_update_id(self) -> int:
        return self.last_update_id + 1


@dataclass(frozen=True, slots=True)
class IngressAcceptance:
    disposition: IngressDisposition
    checkpoint: TelegramIngressCheckpoint


class TelegramIngressReducer:
    def __init__(self, declaration: IngressOwnerDeclaration, checkpoint: TelegramIngressCheckpoint | None = None) -> None:
        self.declaration = declaration
        self.checkpoint = checkpoint or TelegramIngressCheckpoint(
            schema_version=SCHEMA_VERSION,
            bot_token_hash=declaration.bot_token_hash,
            owner_id=declaration.owner_id,
        )
        if self.checkpoint.bot_token_hash != declaration.bot_token_hash or self.checkpoint.owner_id != declaration.owner_id:
            raise TelegramControlContractError("ingress checkpoint owner binding mismatch")

    def accept(self, *, owner_id: str, update_id: int, update_payload_hash: str) -> IngressAcceptance:
        if _require_text("owner_id", owner_id) != self.declaration.owner_id:
            raise TelegramControlContractError("Telegram update presented by non-owner ingress")
        if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
            raise TelegramControlContractError("update_id must be a non-negative integer")
        digest = _require_sha256("update_payload_hash", update_payload_hash)
        seen = dict(self.checkpoint.seen_update_hashes)
        if update_id in seen:
            if seen[update_id] != digest:
                raise TelegramControlContractError("Telegram update_id reused with a different payload")
            return IngressAcceptance(IngressDisposition.DUPLICATE, self.checkpoint)
        if update_id <= self.checkpoint.last_update_id:
            raise TelegramControlContractError("stale unseen Telegram update violates serialized ingress")
        entries = tuple(sorted((*self.checkpoint.seen_update_hashes, (update_id, digest))))[-256:]
        self.checkpoint = TelegramIngressCheckpoint(
            schema_version=SCHEMA_VERSION,
            bot_token_hash=self.checkpoint.bot_token_hash,
            owner_id=self.checkpoint.owner_id,
            last_update_id=update_id,
            seen_update_hashes=entries,
        )
        return IngressAcceptance(IngressDisposition.ACCEPTED, self.checkpoint)


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    outbox_message_id: str
    bot_token_hash: str
    chat_id: str
    message_kind: str
    correlation_id: str
    payload_hash: str
    journal_event_ref: str
    status: OutboxStatus
    attempts: int
    created_at: str
    next_retry_at: str | None = None
    telegram_message_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("outbox_message_id", "chat_id", "message_kind", "correlation_id", "journal_event_ref"):
            object.__setattr__(self, name, _require_text(name, getattr(self, name)))
        object.__setattr__(self, "bot_token_hash", _require_sha256("bot_token_hash", self.bot_token_hash))
        object.__setattr__(self, "payload_hash", _require_sha256("payload_hash", self.payload_hash))
        if self.attempts < 0:
            raise TelegramControlContractError("outbox attempts must be >= 0")
        _parse_utc("created_at", self.created_at)
        if self.next_retry_at is not None:
            _parse_utc("next_retry_at", self.next_retry_at)
        if self.status is OutboxStatus.SENT and not self.telegram_message_id:
            raise TelegramControlContractError("SENT outbox message requires telegram_message_id")
        if self.telegram_message_id is not None:
            object.__setattr__(self, "telegram_message_id", _require_text("telegram_message_id", self.telegram_message_id))

    @classmethod
    def pending(
        cls,
        *,
        outbox_message_id: str,
        bot_token_hash: str,
        chat_id: str,
        message_kind: str,
        correlation_id: str,
        payload_hash: str,
        journal_event_ref: str,
        created_at: str,
    ) -> "OutboxMessage":
        return cls(
            outbox_message_id=outbox_message_id,
            bot_token_hash=bot_token_hash,
            chat_id=chat_id,
            message_kind=message_kind,
            correlation_id=correlation_id,
            payload_hash=payload_hash,
            journal_event_ref=journal_event_ref,
            status=OutboxStatus.PENDING,
            attempts=0,
            created_at=created_at,
        )

    def immutable_identity(self) -> tuple[str, ...]:
        return (
            self.outbox_message_id,
            self.bot_token_hash,
            self.chat_id,
            self.message_kind,
            self.correlation_id,
            self.payload_hash,
            self.journal_event_ref,
            self.created_at,
        )


class OutboxLedger:
    def __init__(self) -> None:
        self._messages: dict[str, OutboxMessage] = {}

    def get(self, outbox_message_id: str) -> OutboxMessage:
        try:
            return self._messages[outbox_message_id]
        except KeyError as exc:
            raise TelegramControlContractError("unknown outbox_message_id") from exc

    def enqueue(self, message: OutboxMessage) -> OutboxMessage:
        existing = self._messages.get(message.outbox_message_id)
        if existing is None:
            if message.status is not OutboxStatus.PENDING or message.attempts != 0:
                raise TelegramControlContractError("new outbox message must start PENDING with zero attempts")
            self._messages[message.outbox_message_id] = message
            return message
        if existing.immutable_identity() != message.immutable_identity():
            raise TelegramControlContractError("outbox_message_id reused with conflicting immutable identity")
        return existing

    def begin_send(self, outbox_message_id: str) -> OutboxMessage:
        message = self.get(outbox_message_id)
        if message.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            raise TelegramControlContractError("outbox message is not eligible for send")
        updated = replace(message, status=OutboxStatus.SENDING, attempts=message.attempts + 1, next_retry_at=None)
        self._messages[outbox_message_id] = updated
        return updated

    def mark_sent(self, outbox_message_id: str, *, telegram_message_id: str) -> OutboxMessage:
        message = self.get(outbox_message_id)
        if message.status is not OutboxStatus.SENDING:
            raise TelegramControlContractError("only SENDING outbox message can become SENT")
        updated = replace(message, status=OutboxStatus.SENT, telegram_message_id=_require_text("telegram_message_id", telegram_message_id), next_retry_at=None)
        self._messages[outbox_message_id] = updated
        return updated

    def recover_interrupted_send(self, outbox_message_id: str) -> OutboxMessage:
        message = self.get(outbox_message_id)
        if message.status is not OutboxStatus.SENDING:
            return message
        updated = replace(message, status=OutboxStatus.RECONCILIATION_REQUIRED)
        self._messages[outbox_message_id] = updated
        return updated

    def reconcile(self, outbox_message_id: str, *, confirmed_sent_message_id: str | None = None, confirmed_absent: bool = False, next_retry_at: str | None = None) -> OutboxMessage:
        message = self.get(outbox_message_id)
        if message.status is not OutboxStatus.RECONCILIATION_REQUIRED:
            raise TelegramControlContractError("reconciliation requires RECONCILIATION_REQUIRED status")
        if bool(confirmed_sent_message_id) == bool(confirmed_absent):
            raise TelegramControlContractError("reconciliation requires exactly one provider outcome")
        if confirmed_sent_message_id:
            updated = replace(message, status=OutboxStatus.SENT, telegram_message_id=_require_text("confirmed_sent_message_id", confirmed_sent_message_id), next_retry_at=None)
        else:
            if next_retry_at is not None:
                _parse_utc("next_retry_at", next_retry_at)
            updated = replace(message, status=OutboxStatus.PENDING, next_retry_at=next_retry_at, telegram_message_id=None)
        self._messages[outbox_message_id] = updated
        return updated

    def mark_failed(self, outbox_message_id: str, *, next_retry_at: str | None) -> OutboxMessage:
        message = self.get(outbox_message_id)
        if message.status is not OutboxStatus.SENDING:
            raise TelegramControlContractError("only SENDING outbox message can become FAILED")
        if next_retry_at is not None:
            _parse_utc("next_retry_at", next_retry_at)
        updated = replace(message, status=OutboxStatus.FAILED, next_retry_at=next_retry_at)
        self._messages[outbox_message_id] = updated
        return updated


def _parse_utc(name: str, value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise TelegramControlContractError(f"{name} must be ISO-8601") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
        raise TelegramControlContractError(f"{name} must be timezone-aware UTC")
    return stamp


@dataclass(frozen=True, slots=True)
class ReleaseCandidateDigest:
    run_id: str
    final_mp4_sha256: str
    delivery_manifest_sha256: str
    capability_manifest_sha256: str
    release_asset_set_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _require_text("run_id", self.run_id))
        for name in (
            "final_mp4_sha256",
            "delivery_manifest_sha256",
            "capability_manifest_sha256",
            "release_asset_set_digest",
        ):
            object.__setattr__(self, name, _require_sha256(name, getattr(self, name)))

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json({
            "run_id": self.run_id,
            "final_mp4_sha256": self.final_mp4_sha256,
            "delivery_manifest_sha256": self.delivery_manifest_sha256,
            "capability_manifest_sha256": self.capability_manifest_sha256,
            "release_asset_set_digest": self.release_asset_set_digest,
        })).hexdigest()


@dataclass(frozen=True, slots=True)
class BoundApproval:
    approval_id: str
    actor_id: str
    update_id: int
    candidate_digest: str
    decision: ApprovalDecision
    journal_event_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "approval_id", _require_text("approval_id", self.approval_id))
        object.__setattr__(self, "actor_id", _require_text("actor_id", self.actor_id))
        object.__setattr__(self, "candidate_digest", _require_sha256("candidate_digest", self.candidate_digest))
        object.__setattr__(self, "journal_event_ref", _require_text("journal_event_ref", self.journal_event_ref))
        if not isinstance(self.update_id, int) or isinstance(self.update_id, bool) or self.update_id < 0:
            raise TelegramControlContractError("approval update_id must be a non-negative integer")


def bind_release_approval(
    candidate: ReleaseCandidateDigest,
    *,
    supplied_candidate_digest: str,
    approval_id: str,
    actor_id: str,
    update_id: int,
    decision: ApprovalDecision,
    journal_event_ref: str,
) -> BoundApproval:
    supplied = _require_sha256("supplied_candidate_digest", supplied_candidate_digest)
    if supplied != candidate.digest:
        raise TelegramControlContractError("approval is bound to a different release candidate digest")
    return BoundApproval(
        approval_id=approval_id,
        actor_id=actor_id,
        update_id=update_id,
        candidate_digest=supplied,
        decision=decision,
        journal_event_ref=journal_event_ref,
    )


def telegram_journal_projection(state: CanonicalRunState) -> dict:
    projection = dict(project_telegram_status(state))
    if projection.get("authority") != "projection_only":
        raise TelegramControlContractError("Telegram projection must never become authoritative")
    projection["ingress_authority"] = "single_webhook_owner"
    projection["outbound_authority"] = "reconciliation_aware_outbox"
    return projection
