from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import planning_capacity_headroom as short_headroom
from scripts import planning_checkpoint_state as durable_state
from scripts import planning_contract_composition_closure as closure
from scripts import planning_stage_contract as stage_contract
from scripts import run124_terminal_provider_recovery as long_recovery
from scripts import short_repair_reset_recovery


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
                closure._save_stage_checkpoint(
                    {"version": 2, "responses": {key: response}}
                )
                disk = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(disk["version"], 1)
                self.assertEqual(disk[closure.STAGE_CACHE_VERSION_FIELD], 2)
                self.assertEqual(disk["responses"], {key: response})

                # The exact authenticated durable owner must accept what Stage writes,
                # through the Run130 public authority wrapper (never the core directly).
                normalized = durable_state._normalize_checkpoint(disk)
                self.assertEqual(normalized["version"], 1)
                self.assertEqual(normalized["responses"], {key: response})

                # Stage authority sees its own logical version after the disk boundary.
                loaded = closure._load_stage_checkpoint()
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
                    closure._load_stage_checkpoint(),
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
                loaded = closure._load_stage_checkpoint()
                self.assertEqual(loaded["responses"], {key: {"run170": True}})
                closure._save_stage_checkpoint(loaded)
                migrated = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(migrated["version"], 1)
                self.assertEqual(migrated[closure.STAGE_CACHE_VERSION_FIELD], 2)

    def test_existing_short_installer_always_activates_shared_closure(self) -> None:
        with patch.object(
            short_repair_reset_recovery,
            "install_planning_contract_composition_closure",
        ) as shared, patch.object(short_repair_reset_recovery, "_INSTALLED", True):
            short_repair_reset_recovery.install_short_repair_reset_recovery()
        shared.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
