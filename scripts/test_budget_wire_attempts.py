from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_level_planner_router as router  # noqa: E402

import isco_video_agent.orchestrator as orchestrator  # noqa: E402
import isco_video_agent.resilient_planner as staged  # noqa: E402
from isco_video_agent.ai_budget import (  # noqa: E402
    AttemptOutcome,
    BudgetLedger,
    Capability,
    Priority,
    PROVIDER_ATTEMPT_HARD_CAP,
    TaskSpec,
    budget_task_scope,
)


def _spec(task_id: str = "OUTLINE_PLAN") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        kind="OUTLINE_PLAN",
        priority=Priority.P0,
        capability=Capability.TEXT,
        max_provider_attempts=8,
        schema_repair_allowed=True,
        local_fallback=False,
        semantic_block_is_final=False,
    )


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers: dict[str, str] = {}
        self.ok = 200 <= status_code < 300

    def json(self) -> dict:
        return self._payload


def _groq_ok() -> _Response:
    return _Response(
        200,
        {
            "choices": [
                {"message": {"content": json.dumps({"ok": True})}}
            ]
        },
    )


class PlannerWireAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        root = Path(self._tmpdir.name)
        gemini_key = root / "gemini-key"
        groq_key = root / "groq-key"
        gemini_key.write_text("fake-gemini", encoding="utf-8")
        groq_key.write_text("fake-groq", encoding="utf-8")

        self._env = patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY_FILE": str(gemini_key),
                "GROQ_API_KEY_FILE": str(groq_key),
            },
            clear=False,
        )
        self._cache = patch.object(router, "CACHE_PATH", root / "planning-checkpoint.json")
        self._interval = patch.object(router, "MIN_PROVIDER_CALL_INTERVAL_SECONDS", 0.0)
        self._persona = patch.object(router, "with_channel_persona", side_effect=lambda prompt: prompt)
        self._env.start()
        self._cache.start()
        self._interval.start()
        self._persona.start()

        self._orig_json_text = staged.json_text
        self._orig_staged_build_plan = staged.build_plan
        self._orig_orchestrator_build_plan = orchestrator.build_plan
        router._last_call_rate_limit_headers.clear()
        router._USED_PROVIDERS.clear()
        router._TELEMETRY.clear()

    def tearDown(self) -> None:
        staged.json_text = self._orig_json_text
        staged.build_plan = self._orig_staged_build_plan
        orchestrator.build_plan = self._orig_orchestrator_build_plan
        self._persona.stop()
        self._interval.stop()
        self._cache.stop()
        self._env.stop()
        self._tmpdir.cleanup()

    def _install(self) -> None:
        router.install_router()

    # Acceptance matrix #1: checkpoint/cache hit means zero provider calls and zero
    # provider-attempt records, while the logical task still exists.
    def test_planner_cache_hit_records_zero_provider_attempts(self) -> None:
        prompt = "cached-prompt"
        model = "gemini-2.5-flash"
        cache_key = hashlib.sha256((model + "\n" + prompt).encode("utf-8")).hexdigest()
        router.CACHE_PATH.write_text(
            json.dumps({"version": 1, "responses": {cache_key: {"cached": True}}}),
            encoding="utf-8",
        )
        ledger = BudgetLedger("film", enforce=False)

        with patch.object(router, "gemini_json_text", side_effect=AssertionError("provider must not be called")):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model=model):
                result = staged.json_text("unused", prompt, model=model)

        self.assertEqual(result, {"cached": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["logical_tasks"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["total"], 0)

    # Acceptance matrix #2: Gemini success = exactly one real attempt.
    def test_planner_gemini_success_records_one_attempt(self) -> None:
        ledger = BudgetLedger("film", enforce=False)
        with patch.object(router, "gemini_json_text", return_value={"ok": True}):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                result = staged.json_text("unused", "prompt", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1})
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"SUCCESS": 1})

    # Acceptance matrix #3: Gemini 429 -> Groq success = exactly two attempts.
    def test_planner_gemini_429_then_groq_success_records_exactly_two(self) -> None:
        ledger = BudgetLedger("film", enforce=False)
        with patch.object(router, "gemini_json_text", side_effect=RuntimeError("HTTP 429 rate limited")), \
             patch.object(router.requests, "post", return_value=_groq_ok()):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                result = staged.json_text("unused", "prompt", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "groq": 1})
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"RATE_LIMITED": 1, "SUCCESS": 1})

    # Acceptance matrix #4: Gemini -> Groq -> OpenRouter success = three attempts.
    def test_planner_three_provider_fallback_records_three_attempts(self) -> None:
        ledger = BudgetLedger("film", enforce=False)
        with patch.object(router, "gemini_json_text", side_effect=TimeoutError("request timed out")), \
             patch.object(router.requests, "post", return_value=_Response(503)), \
             patch.object(router, "openrouter_json_text", return_value={"ok": True}):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                result = staged.json_text("unused", "prompt", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 3)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "groq": 1, "openrouter": 1})

    # Acceptance matrix #5: once Gemini is circuit-open, the next subtask skips it;
    # only Groq's actual request is counted.
    def test_planner_circuit_open_skip_then_groq_counts_only_one(self) -> None:
        with patch.object(router, "gemini_json_text", side_effect=RuntimeError("HTTP 429 rate limited")), \
             patch.object(router.requests, "post", return_value=_groq_ok()):
            self._install()
            # Prime the router's run-local Gemini cooldown without any active ledger.
            staged.json_text("unused", "prime-prompt", model="gemini-2.5-flash")

            ledger = BudgetLedger("film", enforce=False)
            with budget_task_scope(ledger, _spec("SECOND_TASK"), requested_model="gemini-2.5-flash"):
                result = staged.json_text("unused", "second-prompt", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 1)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"groq": 1})

    # Acceptance matrix #6: OpenRouter invalid JSON + one repair request = two
    # OpenRouter provider attempts, not one outer interaction.
    def test_openrouter_invalid_json_repair_records_two_attempts(self) -> None:
        ledger = BudgetLedger("film", enforce=False)
        with patch.object(
            router,
            "openrouter_json_text",
            side_effect=[RuntimeError("OpenRouter returned invalid JSON"), {"ok": True}],
        ):
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                result = router._openrouter_call_with_repair(
                    "prompt", "openrouter/free", "openrouter-free-router"
                )

        self.assertEqual(result, {"ok": True})
        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 2)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"openrouter": 2})
        self.assertEqual(summary["provider_attempts"]["by_outcome"], {"SCHEMA_INVALID": 1, "SUCCESS": 1})

    # Acceptance matrix #7: all four configured provider paths fail technically;
    # Ledger must record all four attempts before the router raises exhaustion.
    def test_all_planner_providers_fail_records_all_attempts(self) -> None:
        ledger = BudgetLedger("film", enforce=False)
        with patch.object(router, "gemini_json_text", side_effect=TimeoutError("request timed out")), \
             patch.object(router.requests, "post", return_value=_Response(503)), \
             patch.object(router, "openrouter_json_text", side_effect=RuntimeError("OpenRouter HTTP 503")):
            self._install()
            with self.assertRaises(RuntimeError):
                with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                    staged.json_text("unused", "prompt", model="gemini-2.5-flash")

        summary = ledger.to_summary()
        self.assertEqual(summary["provider_attempts"]["total"], 4)
        self.assertEqual(summary["provider_attempts"]["by_provider"], {"gemini": 1, "groq": 1, "openrouter": 2})

    # Acceptance matrix #8: a clean build_plan that performs two distinct planning
    # subtasks must be two provider attempts even though orchestrator sees one logical
    # build_plan call.
    def test_planner_two_clean_subtasks_record_two_attempts(self) -> None:
        ledger = BudgetLedger("film", enforce=False)

        def fake_build_plan(api_key, topic, requested_format, content_model, **kwargs):
            del topic, requested_format, kwargs
            staged.json_text(api_key, "subtask-one", model=content_model)
            staged.json_text(api_key, "subtask-two", model=content_model)
            return SimpleNamespace(narrative_format="")

        with patch.object(staged, "build_plan", side_effect=fake_build_plan), \
             patch.object(router, "gemini_json_text", return_value={"ok": True}):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                orchestrator.build_plan("key", "topic", "film", "gemini-2.5-flash")

        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 2)

    # Acceptance matrix #9: one additional internal repair request adds exactly one
    # more provider attempt.
    def test_planner_internal_repair_adds_exactly_one_attempt(self) -> None:
        ledger = BudgetLedger("film", enforce=False)

        def fake_build_plan(api_key, topic, requested_format, content_model, **kwargs):
            del topic, requested_format, kwargs
            staged.json_text(api_key, "subtask-one", model=content_model)
            staged.json_text(api_key, "subtask-two", model=content_model)
            staged.json_text(api_key, "repair-subtask", model=content_model)
            return SimpleNamespace(narrative_format="")

        with patch.object(staged, "build_plan", side_effect=fake_build_plan), \
             patch.object(router, "gemini_json_text", return_value={"ok": True}):
            self._install()
            with budget_task_scope(ledger, _spec(), requested_model="gemini-2.5-flash"):
                orchestrator.build_plan("key", "topic", "film", "gemini-2.5-flash")

        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], 3)

    # Acceptance matrix #14: without an active ledger scope, routing behavior stays
    # unchanged and the provider is still called normally.
    def test_no_active_ledger_keeps_router_behavior_unchanged(self) -> None:
        with patch.object(router, "gemini_json_text", return_value={"ok": True}) as gemini:
            self._install()
            result = staged.json_text("unused", "prompt", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(gemini.call_count, 1)

    def test_authorize_false_blocks_provider_before_call_and_does_not_record_attempt(self) -> None:
        ledger = BudgetLedger("film", enforce=True)
        spec = _spec("CAP_TEST")
        ledger.register_task(spec)
        for index in range(PROVIDER_ATTEMPT_HARD_CAP["film"]):
            ledger.record_attempt(
                spec.task_id,
                provider="seed",
                requested_model="seed",
                resolved_model="seed",
                capability=Capability.TEXT,
                outcome=AttemptOutcome.SUCCESS,
            )

        calls = {"n": 0}

        def provider() -> dict:
            calls["n"] += 1
            return {"ok": True}

        before = ledger.to_summary()["provider_attempts"]["total"]
        with budget_task_scope(ledger, spec, requested_model="gemini-2.5-flash"):
            with self.assertRaisesRegex(RuntimeError, "budget authorization denied"):
                router._budgeted_provider_call(
                    "gemini", "gemini-2.5-flash", provider
                )

        self.assertEqual(calls["n"], 0)
        self.assertEqual(ledger.to_summary()["provider_attempts"]["total"], before)


if __name__ == "__main__":
    unittest.main()