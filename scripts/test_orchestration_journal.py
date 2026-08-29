from __future__ import annotations

import unittest

from scripts.orchestration_journal import (
    JournalContractError,
    JournalEvent,
    JournalEventType,
    JournalReducer,
    RunState,
    StageState,
    project_github_summary,
    project_telegram_status,
)


STAMP = "2026-08-29T18:00:00+00:00"


def stage_event(
    seq: int,
    event_id: str,
    previous: StageState,
    target: StageState,
    *,
    stage_id: str = "media",
    evidence_refs: tuple[str, ...] = (),
) -> JournalEvent:
    return JournalEvent(
        schema_version=1,
        run_id="run-1",
        run_sequence=seq,
        event_id=event_id,
        timestamp_utc=STAMP,
        event_type=JournalEventType.STAGE_STATE_CHANGED,
        contract_id="media-stage-v1",
        stage_id=stage_id,
        attempt=1,
        deadline_remaining_ms=1000,
        previous_state=previous.value,
        next_state=target.value,
        evidence_refs=evidence_refs,
    )


def run_event(
    seq: int,
    event_id: str,
    previous: RunState,
    target: RunState,
) -> JournalEvent:
    return JournalEvent(
        schema_version=1,
        run_id="run-1",
        run_sequence=seq,
        event_id=event_id,
        timestamp_utc=STAMP,
        event_type=JournalEventType.RUN_STATE_CHANGED,
        contract_id="production-run-v1",
        stage_id=None,
        attempt=0,
        deadline_remaining_ms=1000,
        previous_state=previous.value,
        next_state=target.value,
    )


class JournalEventContractTests(unittest.TestCase):
    def test_stage_event_requires_stage_identity_and_positive_attempt(self) -> None:
        with self.assertRaises(JournalContractError):
            JournalEvent(
                schema_version=1,
                run_id="run-1",
                run_sequence=1,
                event_id="e1",
                timestamp_utc=STAMP,
                event_type=JournalEventType.STAGE_STATE_CHANGED,
                contract_id="media-stage-v1",
                stage_id=None,
                attempt=1,
                deadline_remaining_ms=1,
                previous_state="PENDING",
                next_state="ADMISSION_CHECK",
            )
        with self.assertRaises(JournalContractError):
            JournalEvent(
                schema_version=1,
                run_id="run-1",
                run_sequence=1,
                event_id="e1",
                timestamp_utc=STAMP,
                event_type=JournalEventType.STAGE_STATE_CHANGED,
                contract_id="media-stage-v1",
                stage_id="media",
                attempt=0,
                deadline_remaining_ms=1,
                previous_state="PENDING",
                next_state="ADMISSION_CHECK",
            )

    def test_event_requires_utc_nonnegative_budget_and_supported_schema(self) -> None:
        kwargs = dict(
            schema_version=1,
            run_id="run-1",
            run_sequence=1,
            event_id="e1",
            timestamp_utc=STAMP,
            event_type=JournalEventType.RUN_STATE_CHANGED,
            contract_id="production-run-v1",
            stage_id=None,
            attempt=0,
            deadline_remaining_ms=1,
            previous_state=RunState.INITIALIZING.value,
            next_state=RunState.RUNNING.value,
        )
        with self.assertRaises(JournalContractError):
            JournalEvent(**{**kwargs, "schema_version": 2})
        with self.assertRaises(JournalContractError):
            JournalEvent(**{**kwargs, "deadline_remaining_ms": -1})
        with self.assertRaises(JournalContractError):
            JournalEvent(**{**kwargs, "timestamp_utc": "2026-08-29T18:00:00"})


class JournalReducerTests(unittest.TestCase):
    def test_legal_stage_lifecycle_reduces_to_succeeded(self) -> None:
        reducer = JournalReducer("run-1")
        events = (
            stage_event(1, "e1", StageState.PENDING, StageState.ADMISSION_CHECK),
            stage_event(2, "e2", StageState.ADMISSION_CHECK, StageState.ADMITTED),
            stage_event(3, "e3", StageState.ADMITTED, StageState.RUNNING),
            stage_event(4, "e4", StageState.RUNNING, StageState.VALIDATING),
            stage_event(
                5,
                "e5",
                StageState.VALIDATING,
                StageState.SUCCEEDED,
                evidence_refs=("sha256:abc",),
            ),
        )
        for event in events:
            reducer.apply(event)
        self.assertEqual(reducer.state.stage_states["media"], StageState.SUCCEEDED)
        self.assertEqual(reducer.state.last_sequence, 5)
        self.assertEqual(reducer.state.evidence_refs, ("sha256:abc",))

    def test_illegal_transition_is_rejected(self) -> None:
        reducer = JournalReducer("run-1")
        with self.assertRaises(JournalContractError):
            reducer.apply(stage_event(1, "e1", StageState.PENDING, StageState.SUCCEEDED))

    def test_previous_state_mismatch_is_rejected(self) -> None:
        reducer = JournalReducer("run-1")
        reducer.apply(stage_event(1, "e1", StageState.PENDING, StageState.ADMISSION_CHECK))
        with self.assertRaises(JournalContractError):
            reducer.apply(stage_event(2, "e2", StageState.PENDING, StageState.ADMITTED))

    def test_duplicate_event_is_idempotent_but_conflicting_reuse_is_rejected(self) -> None:
        reducer = JournalReducer("run-1")
        first = stage_event(1, "e1", StageState.PENDING, StageState.ADMISSION_CHECK)
        state1 = reducer.apply(first)
        state2 = reducer.apply(first)
        self.assertEqual(state1, state2)
        self.assertEqual(reducer.state.event_count, 1)

        conflicting = stage_event(
            1, "e1", StageState.PENDING, StageState.CANCELLED
        )
        with self.assertRaises(JournalContractError):
            reducer.apply(conflicting)

    def test_sequence_must_be_strictly_monotonic_for_new_events(self) -> None:
        reducer = JournalReducer("run-1")
        with self.assertRaises(JournalContractError):
            reducer.apply(stage_event(2, "e2", StageState.PENDING, StageState.ADMISSION_CHECK))

    def test_replay_is_deterministic(self) -> None:
        events = (
            run_event(1, "r1", RunState.INITIALIZING, RunState.RUNNING),
            stage_event(2, "s1", StageState.PENDING, StageState.ADMISSION_CHECK),
            stage_event(3, "s2", StageState.ADMISSION_CHECK, StageState.ADMITTED),
            stage_event(4, "s3", StageState.ADMITTED, StageState.RUNNING),
        )
        self.assertEqual(JournalReducer.replay(events), JournalReducer.replay(events))

    def test_run_state_transition_contract(self) -> None:
        reducer = JournalReducer("run-1")
        reducer.apply(run_event(1, "r1", RunState.INITIALIZING, RunState.RUNNING))
        reducer.apply(run_event(2, "r2", RunState.RUNNING, RunState.APPROVAL_AWAITING))
        reducer.apply(
            run_event(3, "r3", RunState.APPROVAL_AWAITING, RunState.RELEASE_PENDING)
        )
        reducer.apply(run_event(4, "r4", RunState.RELEASE_PENDING, RunState.RELEASED))
        self.assertEqual(reducer.state.run_state, RunState.RELEASED)
        with self.assertRaises(JournalContractError):
            reducer.apply(run_event(5, "r5", RunState.RELEASED, RunState.RUNNING))

    def test_evidence_event_cannot_smuggle_state_transition(self) -> None:
        reducer = JournalReducer("run-1")
        reducer.apply(stage_event(1, "e1", StageState.PENDING, StageState.ADMISSION_CHECK))
        bad = JournalEvent(
            schema_version=1,
            run_id="run-1",
            run_sequence=2,
            event_id="e2",
            timestamp_utc=STAMP,
            event_type=JournalEventType.STAGE_EVIDENCE_RECORDED,
            contract_id="media-stage-v1",
            stage_id="media",
            attempt=1,
            deadline_remaining_ms=900,
            previous_state=StageState.ADMISSION_CHECK.value,
            next_state=StageState.ADMITTED.value,
            evidence_refs=("sha256:x",),
        )
        with self.assertRaises(JournalContractError):
            reducer.apply(bad)


class ProjectionAdapterTests(unittest.TestCase):
    def test_projections_are_explicitly_non_authoritative_and_deterministic(self) -> None:
        reducer = JournalReducer("run-1")
        reducer.apply(run_event(1, "r1", RunState.INITIALIZING, RunState.RUNNING))
        reducer.apply(stage_event(2, "s1", StageState.PENDING, StageState.ADMISSION_CHECK))
        reducer.apply(stage_event(3, "s2", StageState.ADMISSION_CHECK, StageState.BLOCKED))

        github = project_github_summary(reducer.state)
        telegram = project_telegram_status(reducer.state)
        self.assertEqual(github["authority"], "projection_only")
        self.assertEqual(github["blocking_stage_ids"], ["media"])
        self.assertEqual(telegram["authority"], "projection_only")
        self.assertFalse(telegram["terminal"])
        self.assertEqual(project_github_summary(reducer.state), github)
        self.assertEqual(project_telegram_status(reducer.state), telegram)


if __name__ == "__main__":
    unittest.main()
