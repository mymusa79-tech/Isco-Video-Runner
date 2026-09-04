from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import native_short_stage_contract as short_stage
from scripts import planning_capacity_headroom as short_headroom
from scripts import planning_checkpoint_state as durable_state
from scripts import planning_contract_composition_closure as closure
from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as long_recovery
from scripts import runtime_patch_contracts
from scripts import short_repair_reset_recovery
from scripts import short_stage_retry_composition as retry_composition
from scripts.test_planning_production_contract_v2 import PlanningProductionContractV2Tests


_RUN170_DETAIL = (
    "all providers exhausted after 3/6 attempts: "
    "CAPACITY stage=planning.short_repair provider=gemini Error code: 429 quota exceeded | "
    "CAPACITY stage=planning.short_repair provider=groq "
    "GROQ_TPM_WINDOW_BUSY_PRECHECK model=openai/gpt-oss-120b "
    "remaining=2176 reset_in=42.44s action=provider_evidence_failover_without_partial_retry | "
    "PROVIDER_TRANSIENT stage=planning.short_repair provider=openrouter "
    "OPENROUTER_UNAVAILABLE_THIS_RUN reason=preflight_blocked: key spend capacity exhausted"
)


def _run170_error(code=stage_contract.PlanningErrorCode.PROVIDER_TRANSIENT):
    return stage_contract.PlanningStageError(
        code,
        _RUN170_DETAIL,
        stage_id="planning.short_repair",
    )


class Run170PlanningContractCompositionTests(unittest.TestCase):
    def test_exact_run170_stage_error_exposes_typed_groq_reset_evidence(self) -> None:
        evidence = closure.groq_terminal_reset_evidence(_run170_error())
        self.assertEqual(
            evidence,
            closure.GroqTerminalResetEvidence(
                model_name="openai/gpt-oss-120b",
                reset_seconds=42.44,
            ),
        )

    def test_semantic_failure_never_becomes_capacity_recovery(self) -> None:
        error = _run170_error(stage_contract.PlanningErrorCode.SEMANTIC_INVALID)
        self.assertIsNone(closure.groq_terminal_reset_evidence(error))

    def test_legacy_provider_envelope_remains_supported(self) -> None:
        legacy = RuntimeError(
            "All free providers failed for planning subtask: "
            "groq:GROQ_TPM_WINDOW_BUSY_PRECHECK "
            "model=openai/gpt-oss-120b remaining=1181 reset_in=49.74s "
            "action=provider_evidence_failover_without_partial_retry | "
            "openrouter:OPENROUTER_UNAVAILABLE_THIS_RUN"
        )
        self.assertEqual(
            closure.groq_terminal_reset_evidence(legacy),
            closure.GroqTerminalResetEvidence("openai/gpt-oss-120b", 49.74),
        )

    def test_short_recovery_uses_new_stage_contract_evidence_and_retries_once(self) -> None:
        closure.install_planning_contract_composition_closure()
        calls = 0
        sentinel = {"ok": True}

        def call():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise _run170_error()
            return sentinel

        with patch.object(short_headroom.time, "sleep") as sleep, patch.object(
            short_headroom,
            "_clear_model_window",
        ) as clear:
            result = short_headroom._short_provider_call_with_terminal_recovery(
                call,
                phase="repair",
            )

        self.assertIs(result, sentinel)
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(43.94)
        clear.assert_called_once_with("openai/gpt-oss-120b")

    def test_long_recovery_reads_same_stage_contract_evidence(self) -> None:
        closure.install_planning_contract_composition_closure()
        error = _run170_error()
        self.assertAlmostEqual(long_recovery._remaining_reset_seconds(error), 42.44, places=2)
        self.assertEqual(long_recovery._model_from_error(error), "openai/gpt-oss-120b")

    def test_reset_above_existing_limits_still_fails_fast(self) -> None:
        detail = _RUN170_DETAIL.replace("reset_in=42.44s", "reset_in=60.01s")
        error = stage_contract.PlanningStageError(
            stage_contract.PlanningErrorCode.PROVIDER_TRANSIENT,
            detail,
            stage_id="planning.short_repair",
        )
        self.assertIsNone(closure._short_terminal_reset_evidence(error))
        self.assertIsNone(closure._long_remaining_reset_seconds(error))

    def test_stage_v2_cache_roundtrips_inside_durable_v1_document(self) -> None:
        key = "a" * 64
        response = {"ok": True}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "planning-checkpoint.json"
            with patch.object(stage_contract.router, "CACHE_PATH", path):
                checkpoint_guard._stage_checkpoint_save(
                    {"version": 2, "responses": {key: response}}
                )
                disk = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(disk["version"], 1)
                self.assertEqual(
                    disk[checkpoint_guard.STAGE_CACHE_VERSION_FIELD],
                    2,
                )
                self.assertEqual(disk["responses"], {key: response})

                # The exact authenticated durable owner accepts Stage output through
                # the Run130 wrapper; no compatibility-core import is allowed here.
                normalized = durable_state._normalize_checkpoint(disk)
                self.assertEqual(normalized["version"], 1)
                self.assertEqual(normalized["responses"], {key: response})

                loaded = checkpoint_guard._stage_checkpoint_load()
                self.assertEqual(loaded, {"version": 2, "responses": {key: response}})

    def test_unmarked_historical_v1_checkpoint_never_gains_stage_v2_authority(self) -> None:
        key = "b" * 64
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "planning-checkpoint.json"
            path.write_text(
                json.dumps({"version": 1, "responses": {key: {"legacy": True}}}),
                encoding="utf-8",
            )
            with patch.object(stage_contract.router, "CACHE_PATH", path):
                self.assertEqual(
                    checkpoint_guard._stage_checkpoint_load(),
                    {"version": 2, "responses": {}},
                )

    def test_prefixed_run170_v2_document_is_migrated_on_next_write(self) -> None:
        key = "c" * 64
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "planning-checkpoint.json"
            path.write_text(
                json.dumps({"version": 2, "responses": {key: {"run170": True}}}),
                encoding="utf-8",
            )
            with patch.object(stage_contract.router, "CACHE_PATH", path):
                loaded = checkpoint_guard._stage_checkpoint_load()
                self.assertEqual(loaded["responses"], {key: {"run170": True}})
                checkpoint_guard._stage_checkpoint_save(loaded)
                migrated = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(migrated["version"], 1)
                self.assertEqual(
                    migrated[checkpoint_guard.STAGE_CACHE_VERSION_FIELD],
                    2,
                )

    def test_run127_repository_audit_accepts_checkpoint_assignment_owner(self) -> None:
        result = runtime_patch_contracts.repository_runtime_patch_audit()
        self.assertEqual(result["planning_authority_assignment_violations"], 0)

    def test_existing_short_installer_always_activates_shared_reset_closure(self) -> None:
        with patch.object(
            short_repair_reset_recovery,
            "install_planning_contract_composition_closure",
        ) as shared, patch.object(
            short_repair_reset_recovery,
            "install_short_stage_retry_composition",
        ) as retry_owner, patch.object(short_repair_reset_recovery, "_INSTALLED", True):
            short_repair_reset_recovery.install_short_repair_reset_recovery()
        shared.assert_called_once_with()
        retry_owner.assert_called_once_with()


class Run172ShortStageRetryCompositionTests(unittest.TestCase):
    TOPIC = "كيف تنهض عندما تفقد الدافع تمامًا؟"

    def _state(self) -> dict:
        return {"topic": self.TOPIC, "operations": []}

    def test_exact_run172_repair_retry_reuses_one_named_repair_stage(self) -> None:
        state = self._state()
        current = SimpleNamespace(topic=self.TOPIC)
        with patch.object(
            short_stage,
            "active_short_repair_context",
            return_value=(current, "repair issue"),
        ):
            first = retry_composition._stage_with_retry_identity(
                short_stage._stage_for_operation,
                state,
            )
            token = retry_composition._AUTHORIZED_RETRY.set(True)
            try:
                retry = retry_composition._stage_with_retry_identity(
                    short_stage._stage_for_operation,
                    state,
                )
            finally:
                retry_composition._AUTHORIZED_RETRY.reset(token)

        self.assertEqual(first.stage_id, "planning.short_repair")
        self.assertEqual(retry.stage_id, "planning.short_repair")
        self.assertIs(retry, first)
        self.assertEqual(state["operations"], ["short_repair"])

    def test_draft_retry_reuses_named_draft_then_review_advances_explicitly(self) -> None:
        state = self._state()
        with patch.object(short_stage, "active_short_repair_context", return_value=None):
            with patch.object(
                short_stage,
                "active_planning_operation",
                return_value="short_draft",
            ):
                draft = retry_composition._stage_with_retry_identity(
                    short_stage._stage_for_operation,
                    state,
                )
                token = retry_composition._AUTHORIZED_RETRY.set(True)
                try:
                    draft_retry = retry_composition._stage_with_retry_identity(
                        short_stage._stage_for_operation,
                        state,
                    )
                finally:
                    retry_composition._AUTHORIZED_RETRY.reset(token)

            with patch.object(
                short_stage,
                "active_planning_operation",
                return_value="short_review",
            ):
                review = retry_composition._stage_with_retry_identity(
                    short_stage._stage_for_operation,
                    state,
                )

        self.assertEqual(draft.stage_id, "planning.short_draft")
        self.assertIs(draft_retry, draft)
        self.assertEqual(review.stage_id, "planning.short_review")
        self.assertEqual(state["operations"], ["short_draft", "short_review"])

    def test_retry_operation_mismatch_fails_closed(self) -> None:
        state = self._state()
        with patch.object(short_stage, "active_short_repair_context", return_value=None):
            with patch.object(
                short_stage,
                "active_planning_operation",
                return_value="short_draft",
            ):
                retry_composition._stage_with_retry_identity(
                    short_stage._stage_for_operation,
                    state,
                )

            token = retry_composition._AUTHORIZED_RETRY.set(True)
            try:
                with patch.object(
                    short_stage,
                    "active_planning_operation",
                    return_value="short_review",
                ):
                    with self.assertRaises(stage_contract.PlanningStageError) as captured:
                        retry_composition._stage_with_retry_identity(
                            short_stage._stage_for_operation,
                            state,
                        )
            finally:
                retry_composition._AUTHORIZED_RETRY.reset(token)

        self.assertEqual(
            captured.exception.code,
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        )
        self.assertEqual(
            captured.exception.stage_id,
            "planning.short_retry_operation_mismatch",
        )
        self.assertEqual(state["operations"], ["short_draft"])

    def test_retry_marker_exists_only_for_existing_owner_second_attempt(self) -> None:
        observed: list[bool] = []
        calls = 0

        def target():
            nonlocal calls
            calls += 1
            observed.append(retry_composition.authorized_terminal_retry_active())
            if calls == 1:
                raise RuntimeError("provider reset evidence")
            return {"ok": True}

        def bounded_owner(call, *, phase: str):
            self.assertEqual(phase, "repair")
            try:
                return call()
            except RuntimeError:
                return call()

        result = retry_composition._recovery_with_retry_identity(
            bounded_owner,
            target,
            phase="repair",
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(observed, [False, True])

    def test_unmarked_duplicate_repair_fails_before_identity_can_drift(self) -> None:
        state = self._state()
        current = SimpleNamespace(topic=self.TOPIC)
        with patch.object(
            short_stage,
            "active_short_repair_context",
            return_value=(current, "repair issue"),
        ):
            first = retry_composition._stage_with_retry_identity(
                short_stage._stage_for_operation,
                state,
            )
            with self.assertRaises(stage_contract.PlanningStageError) as captured:
                retry_composition._stage_with_retry_identity(
                    short_stage._stage_for_operation,
                    state,
                )

        self.assertEqual(first.stage_id, "planning.short_repair")
        self.assertEqual(
            captured.exception.code,
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        )
        self.assertEqual(captured.exception.stage_id, "planning.short_operation_sequence")
        self.assertEqual(state["operations"], ["short_repair"])

    def test_composition_refuses_a_third_transport_attempt(self) -> None:
        def always_fails():
            raise RuntimeError("transport failed")

        def broken_owner(call, *, phase: str):
            self.assertEqual(phase, "repair")
            for _ in range(2):
                try:
                    call()
                except RuntimeError:
                    pass
            return call()

        with self.assertRaises(stage_contract.PlanningStageError) as captured:
            retry_composition._recovery_with_retry_identity(
                broken_owner,
                always_fails,
                phase="repair",
            )
        self.assertEqual(
            captured.exception.code,
            stage_contract.PlanningErrorCode.INTERNAL_CONTRACT_ERROR,
        )


if __name__ == "__main__":
    unittest.main()
