from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from provider_failure import classify_provider_failure as base_classify_provider_failure
from scripts import checkpoint_namespace_guard as checkpoint_guard
from scripts import planning_stage_contract as contract
from scripts import run124_terminal_provider_recovery as recovery
from scripts import task_level_planner_router as router


MODEL = "openai/gpt-oss-20b"
RESET_SECONDS = 24.86
RUN124_WAIT_SECONDS = 26.36
GENERIC_COOLDOWN_SECONDS = 30.0
STALE_SECONDS = GENERIC_COOLDOWN_SECONDS - RUN124_WAIT_SECONDS

_TEMPORAL = router.NoWireProviderFailure(
    "GROQ_TPM_WINDOW_BUSY_PRECHECK",
    f"model={MODEL} required=6044 remaining=3118 reset_in={RESET_SECONDS:.2f}s",
)


def _script(ids: list[str]) -> dict:
    return {
        "sections": [
            {"id": section_id, "narration": f"نص {section_id}", "key_point": f"key-{section_id}"}
            for section_id in ids
        ]
    }


class Run270PlanningTemporalCooldownAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "planning-checkpoint.json"
        self.key_path = Path(self.tmp.name) / "gemini-key"
        self.key_path.write_text("fake-key", encoding="utf-8")
        self.env = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(self.key_path)}, clear=False)
        self.env.start()
        self.cache = patch.object(router, "CACHE_PATH", self.cache_path)
        self.cache.start()

        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        self.classifier = patch.object(
            router, "classify_provider_failure", base_classify_provider_failure
        )
        self.classifier.start()
        staged.json_text = lambda *_args, **_kwargs: {}
        router._TELEMETRY.clear()
        router._USED_PROVIDERS.clear()

        self.clock = {"mono": 1000.0}
        self.monotonic = patch.object(
            contract.time, "monotonic", side_effect=lambda: self.clock["mono"]
        )
        self.monotonic.start()
        self.sleep = patch.object(contract.time, "sleep", return_value=None)
        self.sleep.start()

        checkpoint_guard.install_checkpoint_namespace_guard()
        checkpoint_guard.assert_stage_checkpoint_namespace_guarded()
        contract.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        self.classifier.stop()
        self.sleep.stop()
        self.monotonic.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _spec(self, provider: str):
        base = contract.script_stage_spec("full_script", ["s4", "s5", "s6"])
        return replace(
            base,
            provider_policy=replace(
                base.provider_policy,
                providers=(provider,),
                max_attempts_per_provider=1,
                max_total_attempts=1,
                second_pass_after_full_exhaustion=False,
            ),
        )

    def _stage_call(self, provider: str, *, prompt: str = "run-270 full-script batch-2"):
        with patch.object(contract, "_admit_provider", return_value=None), \
                contract.request_stage_scope(self._spec(provider)):
            return staged.json_text("request-key", prompt)

    def test_before_fix_control_reproduces_duplicate_30s_authority(self) -> None:
        calls = {"groq": 0}
        valid = _script(["s4", "s5", "s6"])

        def groq(_prompt):
            calls["groq"] += 1
            if calls["groq"] == 1:
                raise _TEMPORAL
            return valid

        # Emulate pre-fix Stage Contract: the exact Run124-owned no-wire event was
        # treated like every generic retryable no-wire event and armed 30 seconds.
        with patch.object(router, "_groq_call", side_effect=groq), \
                patch.object(
                    contract,
                    "_run124_owned_groq_temporal_prewire_failure",
                    return_value=False,
                ):
            with self.assertRaises(contract.PlanningStageError):
                self._stage_call("groq")

            self.clock["mono"] += RUN124_WAIT_SECONDS
            with self.assertRaises(contract.PlanningStageError):
                self._stage_call("groq")

            # Run124 waited reset_in + 1.5 = 26.36s, but the independent Stage
            # cooldown still has about 3.64s remaining, so no second Groq wire occurs.
            self.assertAlmostEqual(STALE_SECONDS, 3.64, places=2)
            self.assertEqual(calls["groq"], 1)
            self.assertEqual(router._TELEMETRY[-1]["result"], "transient-cooldown")
            self.assertFalse(router._TELEMETRY[-1]["wire_attempted"])

            self.clock["mono"] += STALE_SECONDS + 0.01
            result = self._stage_call("groq")

        self.assertEqual(result, valid)
        self.assertEqual(calls["groq"], 2)

    def test_after_fix_run124_wait_reaches_groq_wire_without_stale_stage_cooldown(self) -> None:
        calls = {"groq": 0}
        valid = _script(["s4", "s5", "s6"])

        def groq(_prompt):
            calls["groq"] += 1
            if calls["groq"] == 1:
                raise _TEMPORAL
            return valid

        capture = io.StringIO()
        with patch.object(router, "_groq_call", side_effect=groq):
            with contextlib.redirect_stdout(capture):
                with self.assertRaises(contract.PlanningStageError):
                    self._stage_call("groq")

            self.assertIn("stage_generic_cooldown_suppressed=true", capture.getvalue())
            self.assertIn("temporal_owner=run124_capacity_recovery", capture.getvalue())
            self.assertIn("wire_attempted=false", capture.getvalue())

            # Exact #270 outer recovery timing: reset 24.86 + existing 1.5 safety.
            self.clock["mono"] += RUN124_WAIT_SECONDS
            result = self._stage_call("groq")

        self.assertEqual(result, valid)
        self.assertEqual(calls["groq"], 2)
        groq_rows = [row for row in router._TELEMETRY if row.get("provider") == "groq"]
        self.assertGreaterEqual(len(groq_rows), 2)
        self.assertFalse(groq_rows[0]["wire_attempted"])
        self.assertIsNone(groq_rows[0]["provider_attempt"])
        self.assertTrue(groq_rows[-1]["wire_attempted"])
        self.assertEqual(groq_rows[-1]["provider_attempt"], 1)

    def test_exact_run124_2486_reset_still_waits_2636_and_retries_same_batch_once(self) -> None:
        failure = RuntimeError(
            "PROVIDER_TRANSIENT stage=planning.full_script "
            "all providers exhausted after 1/8 attempts (wire_only=true): "
            f"groq:GROQ_TPM_WINDOW_BUSY_PRECHECK model={MODEL} "
            f"required=6044 remaining=3118 reset_in={RESET_SECONDS:.2f}s"
        )
        calls: list[tuple[str, ...]] = []

        def fake_call(_api_key, _model, ids, *, prompt_builder, label):
            del prompt_builder, label
            calls.append(tuple(ids))
            if len(calls) == 1:
                raise failure
            return {
                section_id: {"id": section_id, "narration": "ok", "key_point": "ok"}
                for section_id in ids
            }

        original_call = recovery.batching._call_capacity_aware_shard
        had_flag = hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
        original_flag = getattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY", None)
        try:
            recovery.batching._call_capacity_aware_shard = fake_call
            if had_flag:
                delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")
            recovery._RECOVERED_TERMINAL_SHARDS.clear()
            recovery._TERMINAL_RECOVERY_COUNT = 0
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            recovery.install_run124_terminal_provider_recovery()
            with patch.object(recovery.time, "sleep") as sleep:
                result = recovery.batching._call_capacity_aware_shard(
                    "key",
                    MODEL,
                    ["s4", "s5", "s6"],
                    prompt_builder=lambda ids: ",".join(ids),
                    label="writer",
                )
        finally:
            recovery.batching._call_capacity_aware_shard = original_call
            recovery._RECOVERED_TERMINAL_SHARDS.clear()
            recovery._TERMINAL_RECOVERY_COUNT = 0
            recovery._TERMINAL_WAIT_SPENT_SECONDS = 0.0
            if had_flag:
                recovery.batching._ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY = original_flag
            elif hasattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY"):
                delattr(recovery.batching, "_ISCO_RUN124_TERMINAL_PROVIDER_RECOVERY")

        self.assertEqual(set(result), {"s4", "s5", "s6"})
        self.assertEqual(calls, [("s4", "s5", "s6"), ("s4", "s5", "s6")])
        sleep.assert_called_once_with(RUN124_WAIT_SECONDS)

    def test_generic_retryable_no_wire_still_arms_historical_stage_cooldown(self) -> None:
        calls = {"groq": 0}
        valid = _script(["s4", "s5", "s6"])
        generic = router.NoWireProviderFailure(
            "LOCAL_TRANSIENT_TIMEOUT",
            "timeout before transport; no Groq TPM/RPM reset evidence",
        )

        def groq(_prompt):
            calls["groq"] += 1
            if calls["groq"] == 1:
                raise generic
            return valid

        with patch.object(router, "_groq_call", side_effect=groq):
            with self.assertRaises(contract.PlanningStageError):
                self._stage_call("groq")
            self.clock["mono"] += RUN124_WAIT_SECONDS
            with self.assertRaises(contract.PlanningStageError):
                self._stage_call("groq")
            self.assertEqual(calls["groq"], 1)
            self.assertEqual(router._TELEMETRY[-1]["result"], "transient-cooldown")
            self.clock["mono"] += STALE_SECONDS + 0.01
            self.assertEqual(self._stage_call("groq"), valid)

    def test_unbounded_or_unknown_groq_temporal_evidence_keeps_generic_behavior(self) -> None:
        for reset in ("unknown", "60.01"):
            with self.subTest(reset=reset):
                exc = router.NoWireProviderFailure(
                    "GROQ_TPM_WINDOW_BUSY_PRECHECK",
                    f"model={MODEL} required=6044 remaining=3118 reset_in={reset}s",
                )
                _classified, retryable, _retry_after, _failure = contract._safe_provider_failure(
                    contract.bind_request_contract(self._spec("groq"), "prompt"),
                    "groq",
                    exc,
                )
                self.assertFalse(
                    contract._run124_owned_groq_temporal_prewire_failure(
                        "groq",
                        exc,
                        wire_attempted=False,
                        retryable=retryable,
                    )
                )

    def test_gemini_short_window_429_is_not_part_of_run124_suppression(self) -> None:
        exc = RuntimeError(
            "GEMINI_HTTP_429 status=429 quota exceeded requests per minute Retry-After=29.00"
        )
        bound = contract.bind_request_contract(self._spec("gemini"), "prompt")
        _classified, retryable, _retry_after, _failure = contract._safe_provider_failure(
            bound, "gemini", exc
        )
        self.assertEqual(_failure.quota_scope, "short_window")
        self.assertEqual(float(_retry_after), 29.0)
        self.assertFalse(
            contract._run124_owned_groq_temporal_prewire_failure(
                "gemini",
                exc,
                wire_attempted=True,
                retryable=retryable,
            )
        )

    def test_groq_real_http_429_and_daily_quota_are_not_prewire_temporal_events(self) -> None:
        cases = (
            RuntimeError(
                "GROQ_HTTP_429 status=429 rate limit requests per minute Retry-After=29.00"
            ),
            RuntimeError("GROQ_HTTP_429 status=429 daily quota exceeded requests per day"),
        )
        bound = contract.bind_request_contract(self._spec("groq"), "prompt")
        for exc in cases:
            with self.subTest(detail=str(exc)):
                _classified, retryable, _retry_after, _failure = contract._safe_provider_failure(
                    bound, "groq", exc
                )
                self.assertFalse(
                    contract._run124_owned_groq_temporal_prewire_failure(
                        "groq",
                        exc,
                        wire_attempted=True,
                        retryable=retryable,
                    )
                )

    def test_other_providers_and_openrouter_spend_capacity_are_unchanged(self) -> None:
        temporal = router.NoWireProviderFailure(
            "GROQ_TPM_WINDOW_BUSY_PRECHECK",
            f"model={MODEL} reset_in={RESET_SECONDS:.2f}s",
        )
        for provider in ("gemini", "mistral", "openrouter"):
            with self.subTest(provider=provider):
                self.assertFalse(
                    contract._run124_owned_groq_temporal_prewire_failure(
                        provider,
                        temporal,
                        wire_attempted=False,
                        retryable=True,
                    )
                )

        blocked = router.NoWireProviderFailure(
            "OPENROUTER_UNAVAILABLE_THIS_RUN",
            "key spend capacity exhausted",
        )
        bound = contract.bind_request_contract(self._spec("openrouter"), "prompt")
        _classified, retryable, _retry_after, failure = contract._safe_provider_failure(
            bound, "openrouter", blocked
        )
        self.assertTrue(failure.open_circuit)
        self.assertFalse(
            contract._run124_owned_groq_temporal_prewire_failure(
                "openrouter",
                blocked,
                wire_attempted=False,
                retryable=retryable,
            )
        )

    def test_rpm_marker_with_bounded_reset_is_owned_by_run124_too(self) -> None:
        exc = router.NoWireProviderFailure(
            "GROQ_RPM_WINDOW_BUSY_PRECHECK",
            f"model={MODEL} reset_in=12.50s",
        )
        self.assertTrue(
            contract._run124_owned_groq_temporal_prewire_failure(
                "groq",
                exc,
                wire_attempted=False,
                retryable=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
