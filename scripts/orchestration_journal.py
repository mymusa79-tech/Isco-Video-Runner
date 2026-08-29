from __future__ import annotations

"""Canonical Production Run Journal foundation.

This module is deliberately storage-agnostic and is not wired into production yet.
It defines the immutable event contract, deterministic reducer, and read-only status
projections used by later Production Orchestration layers.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Mapping


SCHEMA_VERSION = 1


class JournalContractError(ValueError):
    """Raised when journal input violates the canonical event/state contract."""


class StageState(str, Enum):
    PENDING = "PENDING"
    ADMISSION_CHECK = "ADMISSION_CHECK"
    ADMITTED = "ADMITTED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"
    DEADLINE_EXHAUSTED = "DEADLINE_EXHAUSTED"
    CANCELLED = "CANCELLED"
    RECONCILING = "RECONCILING"


class RunState(str, Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    APPROVAL_AWAITING = "APPROVAL_AWAITING"
    RELEASE_PENDING = "RELEASE_PENDING"
    RECONCILING = "RECONCILING"
    RELEASED = "RELEASED"
    DELIVERY_DEGRADED = "DELIVERY_DEGRADED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class JournalEventType(str, Enum):
    STAGE_STATE_CHANGED = "STAGE_STATE_CHANGED"
    RUN_STATE_CHANGED = "RUN_STATE_CHANGED"
    STAGE_EVIDENCE_RECORDED = "STAGE_EVIDENCE_RECORDED"
    RUN_EVIDENCE_RECORDED = "RUN_EVIDENCE_RECORDED"


class OrchestrationErrorClass(str, Enum):
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    PROVIDER_CAPACITY = "PROVIDER_CAPACITY"
    INVALID_STRUCTURAL = "INVALID_STRUCTURAL"
    INVALID_SEMANTIC = "INVALID_SEMANTIC"
    INVALID_CONTRACT_INPUT = "INVALID_CONTRACT_INPUT"
    INVALID_CACHE = "INVALID_CACHE"
    EXHAUSTED_DEADLINE = "EXHAUSTED_DEADLINE"
    SIDE_EFFECT_RECONCILIATION_REQUIRED = "SIDE_EFFECT_RECONCILIATION_REQUIRED"
    INTERNAL_CONTRACT_ERROR = "INTERNAL_CONTRACT_ERROR"


_STAGE_TRANSITIONS: Mapping[StageState, frozenset[StageState]] = {
    StageState.PENDING: frozenset({StageState.ADMISSION_CHECK, StageState.CANCELLED}),
    StageState.ADMISSION_CHECK: frozenset(
        {
            StageState.ADMITTED,
            StageState.BLOCKED,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
            StageState.CANCELLED,
        }
    ),
    StageState.ADMITTED: frozenset(
        {StageState.RUNNING, StageState.DEADLINE_EXHAUSTED, StageState.CANCELLED}
    ),
    StageState.RUNNING: frozenset(
        {
            StageState.VALIDATING,
            StageState.FAILED_RETRYABLE,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
            StageState.CANCELLED,
            StageState.RECONCILING,
        }
    ),
    StageState.VALIDATING: frozenset(
        {
            StageState.SUCCEEDED,
            StageState.DEGRADED,
            StageState.BLOCKED,
            StageState.FAILED_RETRYABLE,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
            StageState.CANCELLED,
            StageState.RECONCILING,
        }
    ),
    StageState.FAILED_RETRYABLE: frozenset(
        {
            StageState.ADMISSION_CHECK,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
            StageState.CANCELLED,
        }
    ),
    StageState.RECONCILING: frozenset(
        {
            StageState.SUCCEEDED,
            StageState.DEGRADED,
            StageState.BLOCKED,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
            StageState.CANCELLED,
        }
    ),
    StageState.SUCCEEDED: frozenset(),
    StageState.DEGRADED: frozenset(),
    StageState.BLOCKED: frozenset(),
    StageState.FAILED_TERMINAL: frozenset(),
    StageState.DEADLINE_EXHAUSTED: frozenset(),
    StageState.CANCELLED: frozenset(),
}

_RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.INITIALIZING: frozenset(
        {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED, RunState.EXPIRED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.APPROVAL_AWAITING,
            RunState.RECONCILING,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.APPROVAL_AWAITING: frozenset(
        {
            RunState.RELEASE_PENDING,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        }
    ),
    RunState.RELEASE_PENDING: frozenset(
        {
            RunState.RELEASED,
            RunState.RECONCILING,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.RECONCILING: frozenset(
        {
            RunState.RUNNING,
            RunState.RELEASE_PENDING,
            RunState.RELEASED,
            RunState.FAILED,
            RunState.EXPIRED,
        }
    ),
    RunState.RELEASED: frozenset({RunState.DELIVERY_DEGRADED}),
    RunState.DELIVERY_DEGRADED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.EXPIRED: frozenset(),
}

_STAGE_EVENT_TYPES = frozenset(
    {JournalEventType.STAGE_STATE_CHANGED, JournalEventType.STAGE_EVIDENCE_RECORDED}
)
_RUN_EVENT_TYPES = frozenset(
    {JournalEventType.RUN_STATE_CHANGED, JournalEventType.RUN_EVIDENCE_RECORDED}
)


@dataclass(frozen=True, slots=True)
class JournalEvent:
    schema_version: int
    run_id: str
    run_sequence: int
    event_id: str
    timestamp_utc: str
    event_type: JournalEventType
    contract_id: str
    attempt: int
    deadline_remaining_ms: int
    previous_state: str
    next_state: str
    stage_id: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    evidence_refs: tuple[str, ...] = ()
    error_class: OrchestrationErrorClass | None = None
    side_effect_idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise JournalContractError(
                f"unsupported journal schema_version:{self.schema_version}"
            )
        for name, value in (
            ("run_id", self.run_id),
            ("event_id", self.event_id),
            ("contract_id", self.contract_id),
            ("previous_state", self.previous_state),
            ("next_state", self.next_state),
        ):
            if not isinstance(value, str) or not value.strip():
                raise JournalContractError(f"{name} must be a non-empty string")
        if self.run_sequence < 1:
            raise JournalContractError("run_sequence must be >= 1")
        if self.attempt < 0:
            raise JournalContractError("attempt must be >= 0")
        if self.deadline_remaining_ms < 0:
            raise JournalContractError("deadline_remaining_ms must be >= 0")

        try:
            stamp = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise JournalContractError("timestamp_utc must be ISO-8601") from exc
        if stamp.tzinfo is None or stamp.utcoffset() != timezone.utc.utcoffset(stamp):
            raise JournalContractError("timestamp_utc must be timezone-aware UTC")

        if self.event_type in _STAGE_EVENT_TYPES:
            if self.stage_id is None or not self.stage_id.strip():
                raise JournalContractError("stage_id is required for stage events")
            if self.attempt < 1:
                raise JournalContractError("stage events require attempt >= 1")
        elif self.event_type in _RUN_EVENT_TYPES:
            if self.stage_id is not None:
                raise JournalContractError("run events must not set stage_id")
        else:  # pragma: no cover - Enum construction already prevents this.
            raise JournalContractError(f"unsupported event_type:{self.event_type}")

        if any(not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs):
            raise JournalContractError("evidence_refs must contain non-empty locators")

    @classmethod
    def now(
        cls,
        *,
        run_id: str,
        run_sequence: int,
        event_id: str,
        event_type: JournalEventType,
        contract_id: str,
        attempt: int,
        deadline_remaining_ms: int,
        previous_state: str,
        next_state: str,
        stage_id: str | None = None,
        input_hash: str | None = None,
        output_hash: str | None = None,
        evidence_refs: tuple[str, ...] = (),
        error_class: OrchestrationErrorClass | None = None,
        side_effect_idempotency_key: str | None = None,
    ) -> "JournalEvent":
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            run_sequence=run_sequence,
            event_id=event_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            contract_id=contract_id,
            attempt=attempt,
            deadline_remaining_ms=deadline_remaining_ms,
            previous_state=previous_state,
            next_state=next_state,
            stage_id=stage_id,
            input_hash=input_hash,
            output_hash=output_hash,
            evidence_refs=evidence_refs,
            error_class=error_class,
            side_effect_idempotency_key=side_effect_idempotency_key,
        )


@dataclass(frozen=True, slots=True)
class CanonicalRunState:
    run_id: str
    run_state: RunState = RunState.INITIALIZING
    stage_states: Mapping[str, StageState] = field(default_factory=dict)
    last_sequence: int = 0
    event_count: int = 0
    evidence_refs: tuple[str, ...] = ()


class JournalReducer:
    """Deterministically reduces an ordered journal stream into canonical state."""

    def __init__(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise JournalContractError("run_id must be a non-empty string")
        self._state = CanonicalRunState(run_id=run_id)
        self._seen_events: dict[str, JournalEvent] = {}

    @property
    def state(self) -> CanonicalRunState:
        return self._state

    def apply(self, event: JournalEvent) -> CanonicalRunState:
        if event.run_id != self._state.run_id:
            raise JournalContractError(
                f"event run_id mismatch:{event.run_id}!={self._state.run_id}"
            )

        duplicate = self._seen_events.get(event.event_id)
        if duplicate is not None:
            if duplicate != event:
                raise JournalContractError(
                    f"event_id reused with different payload:{event.event_id}"
                )
            return self._state

        expected_sequence = self._state.last_sequence + 1
        if event.run_sequence != expected_sequence:
            raise JournalContractError(
                f"non-monotonic run_sequence:{event.run_sequence}; expected:{expected_sequence}"
            )

        if event.event_type is JournalEventType.STAGE_STATE_CHANGED:
            next_state = self._apply_stage_transition(event)
        elif event.event_type is JournalEventType.RUN_STATE_CHANGED:
            next_state = self._apply_run_transition(event)
        elif event.event_type is JournalEventType.STAGE_EVIDENCE_RECORDED:
            next_state = self._apply_stage_evidence(event)
        elif event.event_type is JournalEventType.RUN_EVIDENCE_RECORDED:
            next_state = self._apply_run_evidence(event)
        else:  # pragma: no cover
            raise JournalContractError(f"unsupported event_type:{event.event_type}")

        self._seen_events[event.event_id] = event
        self._state = CanonicalRunState(
            run_id=next_state.run_id,
            run_state=next_state.run_state,
            stage_states=dict(next_state.stage_states),
            last_sequence=event.run_sequence,
            event_count=next_state.event_count + 1,
            evidence_refs=next_state.evidence_refs,
        )
        return self._state

    def _apply_stage_transition(self, event: JournalEvent) -> CanonicalRunState:
        assert event.stage_id is not None
        current = self._state.stage_states.get(event.stage_id, StageState.PENDING)
        try:
            previous = StageState(event.previous_state)
            target = StageState(event.next_state)
        except ValueError as exc:
            raise JournalContractError("unknown stage state") from exc
        if previous is not current:
            raise JournalContractError(
                f"stage previous_state mismatch:{event.stage_id}:{previous}!={current}"
            )
        if target not in _STAGE_TRANSITIONS[current]:
            raise JournalContractError(
                f"illegal stage transition:{event.stage_id}:{current}->{target}"
            )
        stage_states = dict(self._state.stage_states)
        stage_states[event.stage_id] = target
        return CanonicalRunState(
            run_id=self._state.run_id,
            run_state=self._state.run_state,
            stage_states=stage_states,
            last_sequence=self._state.last_sequence,
            event_count=self._state.event_count,
            evidence_refs=_merge_refs(self._state.evidence_refs, event.evidence_refs),
        )

    def _apply_run_transition(self, event: JournalEvent) -> CanonicalRunState:
        try:
            previous = RunState(event.previous_state)
            target = RunState(event.next_state)
        except ValueError as exc:
            raise JournalContractError("unknown run state") from exc
        if previous is not self._state.run_state:
            raise JournalContractError(
                f"run previous_state mismatch:{previous}!={self._state.run_state}"
            )
        if target not in _RUN_TRANSITIONS[self._state.run_state]:
            raise JournalContractError(
                f"illegal run transition:{self._state.run_state}->{target}"
            )
        return CanonicalRunState(
            run_id=self._state.run_id,
            run_state=target,
            stage_states=dict(self._state.stage_states),
            last_sequence=self._state.last_sequence,
            event_count=self._state.event_count,
            evidence_refs=_merge_refs(self._state.evidence_refs, event.evidence_refs),
        )

    def _apply_stage_evidence(self, event: JournalEvent) -> CanonicalRunState:
        assert event.stage_id is not None
        current = self._state.stage_states.get(event.stage_id, StageState.PENDING)
        if event.previous_state != current.value or event.next_state != current.value:
            raise JournalContractError(
                "stage evidence event cannot change canonical stage state"
            )
        return CanonicalRunState(
            run_id=self._state.run_id,
            run_state=self._state.run_state,
            stage_states=dict(self._state.stage_states),
            last_sequence=self._state.last_sequence,
            event_count=self._state.event_count,
            evidence_refs=_merge_refs(self._state.evidence_refs, event.evidence_refs),
        )

    def _apply_run_evidence(self, event: JournalEvent) -> CanonicalRunState:
        if (
            event.previous_state != self._state.run_state.value
            or event.next_state != self._state.run_state.value
        ):
            raise JournalContractError("run evidence event cannot change canonical run state")
        return CanonicalRunState(
            run_id=self._state.run_id,
            run_state=self._state.run_state,
            stage_states=dict(self._state.stage_states),
            last_sequence=self._state.last_sequence,
            event_count=self._state.event_count,
            evidence_refs=_merge_refs(self._state.evidence_refs, event.evidence_refs),
        )

    @classmethod
    def replay(cls, events: Iterable[JournalEvent]) -> CanonicalRunState:
        materialized = tuple(events)
        if not materialized:
            raise JournalContractError("cannot replay an empty journal without run_id")
        reducer = cls(materialized[0].run_id)
        for event in materialized:
            reducer.apply(event)
        return reducer.state


def _merge_refs(existing: tuple[str, ...], incoming: tuple[str, ...]) -> tuple[str, ...]:
    merged = list(existing)
    seen = set(existing)
    for ref in incoming:
        if ref not in seen:
            seen.add(ref)
            merged.append(ref)
    return tuple(merged)


def project_github_summary(state: CanonicalRunState) -> dict:
    """Read-only GitHub projection. Never authoritative for production state."""
    blocking = sorted(
        stage_id
        for stage_id, stage_state in state.stage_states.items()
        if stage_state
        in {
            StageState.BLOCKED,
            StageState.FAILED_TERMINAL,
            StageState.DEADLINE_EXHAUSTED,
        }
    )
    return {
        "schema_version": 1,
        "authority": "projection_only",
        "source": "canonical_journal_reducer",
        "run_id": state.run_id,
        "run_state": state.run_state.value,
        "last_sequence": state.last_sequence,
        "event_count": state.event_count,
        "blocking_stage_ids": blocking,
        "stage_states": {
            stage_id: stage_state.value
            for stage_id, stage_state in sorted(state.stage_states.items())
        },
    }


def project_telegram_status(state: CanonicalRunState) -> dict:
    """Read-only Telegram projection. Delivery/ingress ownership is added in L6."""
    active = sorted(
        stage_id
        for stage_id, stage_state in state.stage_states.items()
        if stage_state
        in {
            StageState.ADMISSION_CHECK,
            StageState.ADMITTED,
            StageState.RUNNING,
            StageState.VALIDATING,
            StageState.RECONCILING,
        }
    )
    return {
        "schema_version": 1,
        "authority": "projection_only",
        "source": "canonical_journal_reducer",
        "run_id": state.run_id,
        "run_state": state.run_state.value,
        "active_stage_ids": active,
        "last_sequence": state.last_sequence,
        "terminal": state.run_state
        in {
            RunState.RELEASED,
            RunState.DELIVERY_DEGRADED,
            RunState.FAILED,
            RunState.CANCELLED,
            RunState.EXPIRED,
        },
    }
