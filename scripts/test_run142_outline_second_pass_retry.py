from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import isco_video_agent.resilient_planner as staged

from scripts import planning_stage_contract as contract
from scripts import task_level_planner_router as router


def _script(ids: list[str]) -> dict:
    return {
        "sections": [
            {"id": section_id, "narration": f"narration {section_id}", "key_point": f"key-{section_id}"}
            for section_id in ids
        ]
    }


# Run #142 (produce-resilient-v4.yml, 2026-08-31T14:51:32Z, first real production run
# after the outline-split fix): with only one attempt allowed per provider and a total
# budget equal to exactly the provider count, Gemini/Groq/OpenRouter each got exactly one
# shot and all three failed for unrelated transient reasons in the same pass, losing the
# whole stage with zero retry margin. These are the exact production error strings.
_GEMINI_TIMEOUT = "Request timed out. This is a client-side timeout."
_GROQ_JSON_VALIDATE_FAILED = "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
_OPENROUTER_SPEND_BLOCKED = (
    "OPENROUTER_UNAVAILABLE_THIS_RUN reason=preflight_blocked: "
    "openrouter readiness blocked: key spend capacity exhausted"
)


class Run142OutlineSecondPassRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self.tmp.name) / "planning-checkpoint.json"
        self.key_path = Path(self.tmp.name) / "gemini-key"
        self.key_path.write_text("fake-key", encoding="utf-8")
        self.env = patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(self.key_path)},
            clear=False,
        )
        self.env.start()
        self.cache = patch.object(router, "CACHE_PATH", self.cache_path)
        self.cache.start()
        # planning_stage_contract.py and task_level_planner_router.py both do a plain
        # `import time`, so contract.time and router.time are the *same* module object -
        # patching "router.time.sleep" separately would silently clobber a patch already
        # applied via "contract.time.sleep" (whichever context manager is entered last
        # wins), so there must be exactly one patch each for sleep/monotonic here.
        #
        # The round-boundary retry gates re-eligibility on transient_cooldown_until, which
        # is stamped from time.monotonic(). A plain no-op time.sleep() mock (fine for this
        # file's other tests, which never reach the round boundary) would leave every
        # provider looking "still in cooldown" in round 2, since monotonic time barely
        # advances during a mocked sleep. Use a fake clock so sleep() genuinely advances
        # monotonic(), the same causal relationship a real time.sleep(30) has in production.
        self._clock = 1_000.0

        def _fake_monotonic() -> float:
            return self._clock

        def _fake_sleep(seconds: float) -> None:
            self._clock += seconds

        self.time_monotonic = patch.object(contract.time, "monotonic", side_effect=_fake_monotonic)
        self.time_monotonic.start()
        self.time_sleep = patch.object(contract.time, "sleep", side_effect=_fake_sleep)
        self.contract_sleep_mock = self.time_sleep.start()
        self.old_json_text = staged.json_text
        self.old_schema_adapter = router._structured_schema_for_prompt
        staged.json_text = lambda *_args, **_kwargs: {}
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()
        contract.install_planning_contract_router()

    def tearDown(self) -> None:
        staged.json_text = self.old_json_text
        router._structured_schema_for_prompt = self.old_schema_adapter
        self.time_sleep.stop()
        self.time_monotonic.stop()
        self.cache.stop()
        self.env.stop()
        self.tmp.cleanup()

    @staticmethod
    def _second_pass_spec() -> contract.PlanningStageSpec:
        # Exercise the general run_provider_loop() second-pass mechanism (implemented
        # generically on ProviderPolicy, not hardcoded to the outline stage) against a
        # simple script contract, so these tests do not depend on the much larger
        # editorial_outline schema. outline_stage_spec() itself is covered by
        # test_run140_planning_redundancy.py's contract-shape assertions.
        base = contract.script_stage_spec("full_script", ["s1"])
        policy = dataclasses.replace(
            base.provider_policy,
            max_attempts_per_provider=1,
            max_total_attempts=6,
            second_pass_after_full_exhaustion=True,
        )
        return dataclasses.replace(base, provider_policy=policy)

    def test_all_transient_first_sweep_then_second_sweep_succeeds(self) -> None:
        valid = _script(["s1"])
        gemini_calls = 0
        groq_calls = 0
        openrouter_calls = 0

        def fake_gemini(*_args, **_kwargs):
            nonlocal gemini_calls
            gemini_calls += 1
            raise RuntimeError(_GEMINI_TIMEOUT)

        def fake_groq(_prompt):
            nonlocal groq_calls
            groq_calls += 1
            if groq_calls == 1:
                raise RuntimeError(_GROQ_JSON_VALIDATE_FAILED)
            return valid

        def fake_openrouter(*_args, **_kwargs):
            nonlocal openrouter_calls
            openrouter_calls += 1
            raise RuntimeError(_OPENROUTER_SPEND_BLOCKED)

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(self._second_pass_spec()):
            result = staged.json_text("unused", "prompt")

        self.assertEqual(result, valid)
        self.assertEqual(gemini_calls, 2)
        self.assertEqual(groq_calls, 2)
        # OpenRouter's spend-cap block opens its circuit (Fix 2's classification), so the
        # second sweep correctly never re-attempts it within this same request.
        self.assertEqual(openrouter_calls, 1)
        self.contract_sleep_mock.assert_any_call(router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS)

    def test_non_transient_failure_skips_second_pass(self) -> None:
        invalid = _script(["s1"])
        del invalid["sections"][0]["key_point"]
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}

        def fake_gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            return invalid  # structurally invalid: fails validate_response, not transient

        def fake_groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError(_GROQ_JSON_VALIDATE_FAILED)

        def fake_openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            raise RuntimeError(_OPENROUTER_SPEND_BLOCKED)

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(self._second_pass_spec()):
            with self.assertRaises(contract.PlanningStageError):
                staged.json_text("unused", "prompt")

        # Exactly one sweep: a STRUCTURAL_INVALID failure anywhere in the sweep must never
        # earn a second pass, matching the single-shot behavior for definitive failures.
        self.assertEqual(calls, {"gemini": 1, "groq": 1, "openrouter": 1})
        self.contract_sleep_mock.assert_not_called()

    def test_second_sweep_also_fails_raises_after_bounded_two_sweeps(self) -> None:
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}

        def fake_gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)

        def fake_groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)  # any transient, non-circuit-opening reason

        def fake_openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)  # avoid OpenRouter's own circuit-opening path

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(self._second_pass_spec()):
            with self.assertRaisesRegex(contract.PlanningStageError, r"exhausted after 6/6 attempts"):
                staged.json_text("unused", "prompt")

        # Bounded at exactly two full sweeps (max_total_attempts=6, 3 providers each) -
        # never a third, even though every failure stayed PROVIDER_TRANSIENT throughout.
        self.assertEqual(calls, {"gemini": 2, "groq": 2, "openrouter": 2})
        self.assertEqual(self.contract_sleep_mock.call_args_list.count(((router.TRANSIENT_PROVIDER_COOLDOWN_SECONDS,), {})), 1)

    def test_other_stage_types_do_not_get_second_pass_by_default(self) -> None:
        calls = {"gemini": 0, "groq": 0, "openrouter": 0}

        def fake_gemini(*_args, **_kwargs):
            calls["gemini"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)

        def fake_groq(_prompt):
            calls["groq"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)

        def fake_openrouter(*_args, **_kwargs):
            calls["openrouter"] += 1
            raise RuntimeError(_GEMINI_TIMEOUT)

        # Same one-attempt-per-provider shape as _second_pass_spec(), so this isolates
        # exactly the round-boundary behavior - the only difference is the opt-in flag.
        base = contract.script_stage_spec("full_script", ["s1"])
        policy = dataclasses.replace(
            base.provider_policy,
            max_attempts_per_provider=1,
            max_total_attempts=6,
            second_pass_after_full_exhaustion=False,
        )
        spec = dataclasses.replace(base, provider_policy=policy)
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=fake_openrouter), \
                contract.request_stage_scope(spec):
            with self.assertRaisesRegex(contract.PlanningStageError, r"exhausted after 3/6 attempts"):
                staged.json_text("unused", "prompt")

        # No second sweep without the opt-in, even though the whole first sweep was
        # PROVIDER_TRANSIENT and there was plenty of total_attempts budget left (3 of 6).
        self.assertEqual(calls, {"gemini": 1, "groq": 1, "openrouter": 1})
        self.contract_sleep_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
