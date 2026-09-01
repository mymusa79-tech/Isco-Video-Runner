from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_level_planner_router as router  # noqa: E402  (needs sys.path fixup above)
from provider_failure import classify_provider_failure  # noqa: E402


def _central_telemetry_result(detail: str) -> str:
    return classify_provider_failure("unknown", detail).telemetry_result

import isco_video_agent.orchestrator as orchestrator  # noqa: E402
import isco_video_agent.resilient_planner as staged  # noqa: E402


class PersonaInjectionFallbackTests(unittest.TestCase):
    """Covers the fix for the channel-identity fallback gap: task_router() must apply
    with_channel_persona() once, before the provider loop, so Groq/OpenRouter fallback
    prompts carry the same channel identity Gemini prompts always did."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        self._gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(self._gemini_key_path)},
            clear=False,
        )
        self._env_patch.start()
        # Route the planning checkpoint cache to a scratch file so tests never touch/
        # require the real state/ directory and never leak between test runs.
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract router
        # (see ExplicitStageContractOwnershipTests) - correct in production, but it
        # means these task_router-focused tests must force a clean, unmarked baseline
        # of their own so install_router() actually installs task_router as they expect.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_groq_fallback_after_gemini_failure_still_receives_persona(self) -> None:
        captured: dict[str, str] = {}

        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            raise RuntimeError("HTTP 429 rate limited")

        def fake_groq_call(prompt):
            captured["prompt"] = prompt
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار للحلقة", model="gemini-2.5-flash")

        self.assertIn("prompt", captured)
        self.assertIn("<CHANNEL_PERSONA>", captured["prompt"])

    def test_gemini_success_has_no_double_injection(self) -> None:
        captured: dict[str, str] = {}

        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, model
            captured["prompt"] = prompt
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار للحلقة", model="gemini-2.5-flash")

        self.assertIn("prompt", captured)
        self.assertEqual(captured["prompt"].count("<CHANNEL_PERSONA>"), 1)

    def test_legacy_router_also_uses_request_key_after_secret_file_consumption(self) -> None:
        captured: dict[str, str] = {}

        def fake_gemini_json_text(api_key, prompt, model):
            captured.update(api_key=api_key, prompt=prompt, model=model)
            return {"ok": True}

        os.environ.pop("GEMINI_API_KEY_FILE", None)
        self._gemini_key_path.unlink()
        with patch.object(
            router,
            "_read_secret_file",
            side_effect=AssertionError("router install must not re-read a consumed Gemini file"),
        ), patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            result = staged.json_text(
                "request-scoped-key",
                "نداء اليقظة: موضوع اختبار للحلقة",
                model="gemini-2.5-flash",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["api_key"], "request-scoped-key")


class OpenRouterRepairAttemptTests(unittest.TestCase):
    """Covers item 3: a single repair retry when OpenRouter returns a completion body
    that isn't valid JSON (the diagnosed root cause of that provider's failures - never
    429/quota, always a malformed response), asking the same model to reformat its own
    output. Exactly one extra attempt, then failover - no loop, no repeated repairs."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract
        # router (see ExplicitStageContractOwnershipTests) - correct in production,
        # but it means these task_router-focused tests must force a clean, unmarked
        # baseline of their own so install_router() actually installs task_router.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_success_on_first_try_makes_only_one_call(self) -> None:
        calls: list[str] = []

        def fake_openrouter(prompt, model):
            calls.append(prompt)
            return {"ok": True}

        with patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            result = router._openrouter_call_with_repair("original prompt", "openrouter/free")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, ["original prompt"])

    def test_invalid_json_triggers_exactly_one_repair_attempt_that_succeeds(self) -> None:
        calls: list[str] = []

        def fake_openrouter(prompt, model):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("OpenRouter returned invalid JSON")
            return {"ok": True}

        with patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            result = router._openrouter_call_with_repair("original prompt", "openrouter/free")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "original prompt")
        self.assertTrue(calls[1].startswith("original prompt"))
        self.assertIn("JSON", calls[1])
        self.assertNotEqual(calls[1], calls[0])

    def test_repair_attempt_also_failing_propagates_without_a_third_try(self) -> None:
        calls: list[str] = []

        def fake_openrouter(prompt, model):
            calls.append(prompt)
            raise RuntimeError("OpenRouter returned invalid JSON")

        with patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            with self.assertRaisesRegex(RuntimeError, "OpenRouter returned invalid JSON"):
                router._openrouter_call_with_repair("original prompt", "openrouter/free")

        self.assertEqual(len(calls), 2)

    def test_non_json_failure_is_not_repaired_and_propagates_immediately(self) -> None:
        calls: list[str] = []

        def fake_openrouter(prompt, model):
            calls.append(prompt)
            raise RuntimeError("OpenRouter HTTP 500")

        with patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            with self.assertRaisesRegex(RuntimeError, "OpenRouter HTTP 500"):
                router._openrouter_call_with_repair("original prompt", "openrouter/free")

        self.assertEqual(len(calls), 1)

    def test_repair_is_wired_into_the_real_provider_loop_via_task_router(self) -> None:
        calls: list[str] = []

        def always_fail(*a, **k):
            raise RuntimeError("HTTP 429 rate limited")

        def fake_openrouter(prompt, model):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("OpenRouter returned invalid JSON")
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=always_fail), \
                patch.object(router, "_groq_call", side_effect=always_fail), \
                patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            router.install_router()
            result = staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)


class CooldownAwareRoutingTests(unittest.TestCase):
    """Covers item 4: once a provider is circuit-opened (429/quota match) within a
    run, task_router() must never attempt it again for the rest of that run - skip
    straight to the next provider instead of wasting a request on one already known to
    be unavailable. Verified: the `if name in cooldown: continue` check already runs
    before the sleep/last_call_at bookkeeping and before any provider call, so this is
    a regression guard on already-correct behavior, not a behavior change - it fails
    loudly if that check is ever removed or reordered after the call attempt."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract router
        # (see ExplicitStageContractOwnershipTests) - correct in production, but it
        # means these task_router-focused tests must force a clean, unmarked baseline
        # of their own so install_router() actually installs task_router as they expect.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_second_subtask_never_reattempts_a_cooled_down_provider(self) -> None:
        gemini_calls = 0

        def fake_gemini_json_text(api_key, prompt, model):
            nonlocal gemini_calls
            gemini_calls += 1
            raise RuntimeError("HTTP 429 rate limited")

        groq_calls = 0

        def fake_groq_call(prompt):
            nonlocal groq_calls
            groq_calls += 1
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الأول", model="gemini-2.5-flash")
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الثاني", model="gemini-2.5-flash")

        # gemini fails once on subtask 1 and gets circuit-opened; subtask 2 must skip
        # it entirely rather than wasting another attempt, so groq carries both.
        self.assertEqual(gemini_calls, 1)
        self.assertEqual(groq_calls, 2)


class ProviderFailureBookkeepingResilienceTests(unittest.TestCase):
    """Regression for a real 2026-09-01 production failure: a Short repair subtask
    exhausted Gemini/Groq/OpenRouter and the run crashed with
    RuntimeError("... {'type': 'KeyError'}") instead of the expected clean
    "all providers failed" RuntimeError. classify_provider_failure and _record_attempt
    are both live-patched by several layered capacity/latency installers
    (run120/122/123/124/125); a defect in any of those wrappers must never be able to
    crash the planning subtask it is only supposed to be observing - the same principle
    every resilient retry/circuit-breaker implementation follows for its own telemetry.
    This proves task_router degrades to a generic classification/skips telemetry
    instead of propagating, whichever of those wrappers is the one that misbehaves."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_broken_classifier_degrades_to_generic_failure_instead_of_crashing(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            raise RuntimeError("some real provider failure")

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=RuntimeError("groq also failed")), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=RuntimeError("openrouter also failed")), \
                patch.object(router, "classify_provider_failure", side_effect=KeyError("some_unexpected_key")):
            router.install_router()
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")

    def test_broken_telemetry_recorder_degrades_instead_of_crashing(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            raise RuntimeError("some real provider failure")

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=RuntimeError("groq also failed")), \
                patch.object(router, "_openrouter_call_with_repair", side_effect=RuntimeError("openrouter also failed")), \
                patch.object(router, "_record_attempt", side_effect=KeyError("some_unexpected_key")):
            router.install_router()
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")


class NativeShortPlanShapeValidationTests(unittest.TestCase):
    """Regression for a real 2026-09-01 production failure: a native Short subtask got a
    syntactically valid but structurally incomplete plan back from a provider (observed:
    OpenRouter, after "Planning subtask provider selected: openrouter" printed as a
    success) and the run still crashed later with
    RuntimeError("...{'type': 'KeyError'}") from isco_video_agent/planner.py's unguarded
    d["sections"]/s["id"] access, instead of falling back to another provider like every
    other malformed-response case already does. task_router must now reject a
    plan-shaped response missing/malformed "sections" *inside* the retry loop's own try
    block, so it is treated exactly like any other provider failure."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    _VALID_PLAN = {
        "pillar": "understand",
        "hook": "hook text",
        "title_options": ["a"],
        "thumbnail_concepts": ["b"],
        "sections": [{"id": "s1", "narration": "n"}],
        "cta": "cta text",
        "closing_payoff": "payoff text",
    }

    def test_response_missing_sections_key_falls_back_to_next_provider(self) -> None:
        malformed = {k: v for k, v in self._VALID_PLAN.items() if k != "sections"}

        def fake_gemini_json_text(api_key, prompt, model):
            return malformed

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", return_value=dict(self._VALID_PLAN)):
            router.install_router()
            result = staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")
            self.assertEqual(result["sections"], self._VALID_PLAN["sections"])

    def test_section_missing_id_falls_back_to_next_provider(self) -> None:
        malformed = dict(self._VALID_PLAN)
        malformed["sections"] = [{"narration": "no id here"}]

        def fake_gemini_json_text(api_key, prompt, model):
            return malformed

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", return_value=dict(self._VALID_PLAN)):
            router.install_router()
            result = staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")
            self.assertEqual(result["sections"], self._VALID_PLAN["sections"])

    def test_all_providers_returning_malformed_plans_raises_all_providers_failed(self) -> None:
        malformed = {k: v for k, v in self._VALID_PLAN.items() if k != "sections"}

        def fake_gemini_json_text(api_key, prompt, model):
            return malformed

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", return_value=dict(malformed)), \
                patch.object(router, "_openrouter_call_with_repair", return_value=dict(malformed)):
            router.install_router()
            with self.assertRaisesRegex(RuntimeError, "All free providers failed"):
                staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")

    def test_non_plan_shaped_response_is_unaffected(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            result = staged.json_text("unused-api-key", "نداء اليقظة: طلب اختبار", model="gemini-2.5-flash")
            self.assertEqual(result, {"ok": True})


class NativeShortPlanShapeUnitTests(unittest.TestCase):
    def test_looks_like_plan_shaped_response_requires_at_least_two_marker_keys(self) -> None:
        self.assertFalse(router._looks_like_plan_shaped_response({"hook": "x"}))
        self.assertTrue(router._looks_like_plan_shaped_response({"hook": "x", "cta": "y"}))

    def test_validate_plan_shaped_sections_passes_through_non_plan_data(self) -> None:
        data = {"ok": True}
        self.assertEqual(router._validate_plan_shaped_sections(data), data)

    def test_validate_plan_shaped_sections_rejects_missing_sections(self) -> None:
        data = {"hook": "x", "cta": "y", "pillar": "understand"}
        with self.assertRaisesRegex(RuntimeError, "NATIVE_SHORT_PLAN_SECTIONS_MISSING"):
            router._validate_plan_shaped_sections(data)

    def test_validate_plan_shaped_sections_rejects_sections_without_id(self) -> None:
        data = {"hook": "x", "cta": "y", "pillar": "understand", "sections": [{"narration": "n"}]}
        with self.assertRaisesRegex(RuntimeError, "NATIVE_SHORT_PLAN_SECTIONS_MALFORMED"):
            router._validate_plan_shaped_sections(data)

    def test_validate_plan_shaped_sections_accepts_a_well_formed_plan(self) -> None:
        data = dict(NativeShortPlanShapeValidationTests._VALID_PLAN)
        self.assertEqual(router._validate_plan_shaped_sections(data), data)


class UsedProvidersTrackingTests(unittest.TestCase):
    """Covers item 1 of the plan_source request: get_used_providers() must reflect
    exactly which provider(s) actually produced planning output for the current run,
    normalized (both OpenRouter variants collapse to "openrouter") and deduplicated,
    reset fresh on every install_router() call. This is read back by run_v3_voice.py
    after produce() to tag plan.json/quality-final.json with plan_source."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract
        # router (see ExplicitStageContractOwnershipTests) - correct in production,
        # but it means these task_router-focused tests must force a clean, unmarked
        # baseline of their own so install_router() actually installs task_router.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_empty_before_any_subtask_call(self) -> None:
        router.install_router()
        self.assertEqual(router.get_used_providers(), [])

    def test_records_the_provider_that_actually_succeeded(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        self.assertEqual(router.get_used_providers(), ["gemini"])

    def test_only_the_provider_that_succeeded_is_recorded_not_the_one_that_failed(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            raise RuntimeError("HTTP 429 rate limited")

        def fake_groq_call(prompt):
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        self.assertEqual(router.get_used_providers(), ["groq"])

    def test_both_openrouter_variants_normalize_to_one_name(self) -> None:
        def always_fail(*a, **k):
            raise RuntimeError("HTTP 429 rate limited")

        def fake_openrouter(prompt, model):
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=always_fail), \
                patch.object(router, "_groq_call", side_effect=always_fail), \
                patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        self.assertEqual(router.get_used_providers(), ["openrouter"])

    def test_repeated_success_via_same_provider_is_not_duplicated(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الأول", model="gemini-2.5-flash")
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الثاني", model="gemini-2.5-flash")

        self.assertEqual(router.get_used_providers(), ["gemini"])

    def test_fresh_install_router_call_resets_the_list(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")
            self.assertEqual(router.get_used_providers(), ["gemini"])

            router.install_router()
            self.assertEqual(router.get_used_providers(), [])


class ProviderRateLimitSpacingTests(unittest.TestCase):
    """Covers the fix for run 31870521024's Gemini/Groq 429s: a film's 8 sections mean
    many back-to-back planning subtasks in one run, and task_router() previously fired
    them at the same provider with zero delay. This enforces a floor
    (MIN_PROVIDER_CALL_INTERVAL_SECONDS) between two calls to the SAME provider within
    one run, without touching cooldown or provider-selection logic at all - verified
    here by asserting cooldown/provider-order behavior is untouched while sleep is
    only invoked when two calls to the same provider land closer together than the
    floor."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract
        # router (see ExplicitStageContractOwnershipTests) - correct in production,
        # but it means these task_router-focused tests must force a clean, unmarked
        # baseline of their own so install_router() actually installs task_router.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_second_call_to_same_provider_sleeps_for_the_remaining_gap(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        # First subtask's provider attempt reads monotonic() three times (gap check,
        # last_call_at update, then telemetry duration) at t=1000.0; the second
        # subtask's attempt lands 0.1s later at t=1000.1, which is inside the 1.5s
        # floor, so it must sleep for the remaining 1.4s.
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router.time, "monotonic", side_effect=[1000.0, 1000.0, 1000.0, 1000.1, 1000.1, 1000.1]), \
                patch.object(router.time, "sleep") as mock_sleep:
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الأول", model="gemini-2.5-flash")
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الثاني", model="gemini-2.5-flash")

        mock_sleep.assert_called_once()
        (slept_seconds,), _ = mock_sleep.call_args
        self.assertAlmostEqual(slept_seconds, router.MIN_PROVIDER_CALL_INTERVAL_SECONDS - 0.1, places=6)

    def test_first_call_to_a_provider_never_sleeps(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router.time, "sleep") as mock_sleep:
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار فردي", model="gemini-2.5-flash")

        mock_sleep.assert_not_called()


class RouterInstalledMarkerTests(unittest.TestCase):
    """Covers run 31869763274: orchestrator.py's own guard raised even when
    install_router() had genuinely succeeded, because it compared
    build_plan.__module__ instead of checking a marker on the live callable. The
    verification done when that guard was first added simulated "installed" as
    `orchestrator.build_plan = staged.build_plan` (direct assignment) - not what
    install_router() actually does (installs a *wrapper*, routed_build_plan, defined
    in this module) - which is exactly why it gave false confidence. This test calls
    the real install_router() function, not a hand-rolled substitute, and then calls
    orchestrator's own real guard function against whatever it actually installed."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract
        # router (see ExplicitStageContractOwnershipTests) - correct in production,
        # but it means these task_router-focused tests must force a clean, unmarked
        # baseline of their own so install_router() actually installs task_router.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_real_install_router_satisfies_orchestrators_own_guard(self) -> None:
        router.install_router()
        self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
        orchestrator._verify_resilient_router_installed()  # must not raise


class ClassifyFailureTests(unittest.TestCase):
    """Covers the planning-telemetry request: every failed attempt's error_detail must
    bucket into one of a small set of stable result codes instead of a raw, unbounded
    exception string, so runs can be compared/aggregated across time."""

    def test_429_in_detail_classifies_as_429(self) -> None:
        self.assertEqual(_central_telemetry_result("HTTP 429 rate limited"), "429")

    def test_quota_wording_classifies_as_429(self) -> None:
        self.assertEqual(_central_telemetry_result("Resource exhausted: quota exceeded"), "429")

    def test_invalid_json_classifies_as_invalid_json(self) -> None:
        self.assertEqual(_central_telemetry_result("Provider returned invalid JSON"), "invalid_json")

    def test_premature_wording_classifies_as_premature_response(self) -> None:
        self.assertEqual(_central_telemetry_result("premature response truncated"), "premature_response")

    def test_unrecognized_detail_classifies_as_other(self) -> None:
        self.assertEqual(_central_telemetry_result("connection reset by peer"), "network_error")


class ExtractRateLimitHeadersTests(unittest.TestCase):
    def test_pulls_the_three_known_header_names(self) -> None:
        headers = {
            "Retry-After": "30",
            "X-RateLimit-Remaining-Requests": "5",
            "X-RateLimit-Remaining-Tokens": "1000",
            "Some-Other-Header": "ignored",
        }
        self.assertEqual(
            router._extract_rate_limit_headers(headers),
            {"retry_after": "30", "remaining_requests": "5", "remaining_tokens": "1000"},
        )

    def test_missing_headers_come_back_as_none(self) -> None:
        self.assertEqual(
            router._extract_rate_limit_headers({}),
            {"retry_after": None, "remaining_requests": None, "remaining_tokens": None},
        )


class RecordAttemptAndTelemetryTests(unittest.TestCase):
    """Covers _record_attempt()/get_telemetry()/write_planning_telemetry() directly,
    independent of the full task_router() provider loop."""

    def setUp(self) -> None:
        router._TELEMETRY.clear()
        router._last_call_rate_limit_headers.clear()

    def tearDown(self) -> None:
        router._TELEMETRY.clear()
        router._last_call_rate_limit_headers.clear()

    def test_recorded_entry_has_all_expected_fields(self) -> None:
        router._record_attempt("groq", "success", duration_seconds=1.25)
        entries = router.get_telemetry()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["provider"], "groq")
        self.assertEqual(entry["result"], "success")
        self.assertEqual(entry["duration_seconds"], 1.25)
        self.assertIsNone(entry["error_detail"])
        self.assertIn("timestamp", entry)

    def test_pending_rate_limit_headers_are_attached_and_then_cleared(self) -> None:
        router._last_call_rate_limit_headers.update({
            "retry_after": "30",
            "remaining_requests": "5",
            "remaining_tokens": "1000",
        })
        router._record_attempt("groq", "429", error_detail="HTTP 429 rate limited")
        entry = router.get_telemetry()[0]
        self.assertEqual(entry["retry_after"], "30")
        self.assertEqual(entry["remaining_requests"], "5")
        self.assertEqual(entry["remaining_tokens"], "1000")
        # Headers must not leak onto the next, unrelated attempt.
        router._record_attempt("gemini", "success")
        second_entry = router.get_telemetry()[1]
        self.assertIsNone(second_entry["retry_after"])

    def test_get_telemetry_returns_a_copy_not_the_live_list(self) -> None:
        router._record_attempt("gemini", "success")
        snapshot = router.get_telemetry()
        snapshot.append({"provider": "fake", "result": "success"})
        self.assertEqual(len(router.get_telemetry()), 1)

    def test_write_planning_telemetry_writes_expected_structure(self) -> None:
        router._record_attempt("gemini", "429", error_detail="HTTP 429 rate limited")
        router._record_attempt("groq", "success", duration_seconds=2.0)
        router._record_attempt("groq", "success", duration_seconds=1.0)
        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            path = router.write_planning_telemetry(out_dir)
            self.assertEqual(path, out_dir / "planning-telemetry.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("generated_at", payload)
        self.assertEqual(len(payload["attempts"]), 3)
        self.assertEqual(payload["providers"]["gemini"]["total_attempts"], 1)
        self.assertEqual(payload["providers"]["gemini"]["by_result"], {"429": 1})
        self.assertEqual(payload["providers"]["groq"]["total_attempts"], 2)
        self.assertEqual(payload["providers"]["groq"]["by_result"], {"success": 2})


class TelemetryRouterIntegrationTests(unittest.TestCase):
    """Covers task_router()'s actual instrumentation points: circuit-open skips,
    successful calls, and failed calls must each leave exactly one telemetry entry,
    and install_router() must reset telemetry the same way it resets _USED_PROVIDERS."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()
        # In the full test-suite process, another module's test may have left
        # isco_video_agent.resilient_planner.json_text pointed at
        # planning_stage_contract.py's contract_router. install_router() now
        # deliberately never overwrites an already-live explicit Stage Contract router
        # (see ExplicitStageContractOwnershipTests) - correct in production, but it
        # means these task_router-focused tests must force a clean, unmarked baseline
        # of their own so install_router() actually installs task_router as they expect.
        self._json_text_patch = patch.object(staged, "json_text", lambda *a, **k: {})
        self._json_text_patch.start()

    def tearDown(self) -> None:
        self._json_text_patch.stop()
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_install_router_resets_telemetry(self) -> None:
        router._TELEMETRY.append({"provider": "stale", "result": "success"})
        router.install_router()
        self.assertEqual(router.get_telemetry(), [])

    def test_successful_call_records_one_success_entry(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        entries = router.get_telemetry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["provider"], "gemini")
        self.assertEqual(entries[0]["result"], "success")
        self.assertIsNotNone(entries[0]["duration_seconds"])

    def test_failed_then_fallback_records_a_failure_entry_and_a_success_entry(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            raise RuntimeError("Provider returned invalid JSON")

        def fake_groq_call(prompt):
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        entries = router.get_telemetry()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["provider"], "gemini")
        self.assertEqual(entries[0]["result"], "invalid_json")
        self.assertEqual(entries[0]["error_detail"], "Provider returned invalid JSON")
        self.assertEqual(entries[1]["provider"], "groq")
        self.assertEqual(entries[1]["result"], "success")

    def test_cooled_down_provider_records_circuit_open_not_another_failure(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            raise RuntimeError("HTTP 429 rate limited")

        def fake_groq_call(prompt):
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router, "_groq_call", side_effect=fake_groq_call):
            router.install_router()
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الأول", model="gemini-2.5-flash")
            staged.json_text("unused-api-key", "نداء اليقظة: القسم الثاني", model="gemini-2.5-flash")

        entries = router.get_telemetry()
        # Subtask 1: gemini fails (429) then groq succeeds. Subtask 2: gemini is
        # skipped outright (circuit-open) then groq succeeds again.
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0]["provider"], "gemini")
        self.assertEqual(entries[0]["result"], "429")
        self.assertEqual(entries[1]["provider"], "groq")
        self.assertEqual(entries[1]["result"], "success")
        self.assertEqual(entries[2]["provider"], "gemini")
        self.assertEqual(entries[2]["result"], "circuit-open")
        self.assertEqual(entries[3]["provider"], "groq")
        self.assertEqual(entries[3]["result"], "success")

    def test_groq_429_response_headers_are_captured_on_the_failure_entry(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            raise RuntimeError("HTTP 429 rate limited")

        fake_response = type(
            "FakeResponse",
            (),
            {
                "status_code": 429,
                "headers": {
                    "Retry-After": "12",
                    "X-RateLimit-Remaining-Requests": "0",
                    "X-RateLimit-Remaining-Tokens": "0",
                },
                "ok": False,
            },
        )()

        groq_key_path = Path(self._tmpdir.name) / "groq_key"
        groq_key_path.write_text("fake-groq-key", encoding="utf-8")

        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.dict(os.environ, {"GROQ_API_KEY_FILE": str(groq_key_path)}, clear=False), \
                patch.object(router.requests, "post", return_value=fake_response):
            router.install_router()
            with self.assertRaises(RuntimeError):
                staged.json_text("unused-api-key", "نداء اليقظة: موضوع اختبار", model="gemini-2.5-flash")

        entries = router.get_telemetry()
        groq_entry = next(e for e in entries if e["provider"] == "groq")
        self.assertEqual(groq_entry["result"], "429")
        self.assertEqual(groq_entry["retry_after"], "12")
        self.assertEqual(groq_entry["remaining_requests"], "0")
        self.assertEqual(groq_entry["remaining_tokens"], "0")


class ExplicitStageContractOwnershipTests(unittest.TestCase):
    """Two independent 'try Gemini, then Groq, then OpenRouter' provider loops used to
    both install themselves onto isco_video_agent.resilient_planner.json_text - this
    module's own task_router (below) and planning_stage_contract.py's contract_router -
    so live behavior depended on whichever installer happened to run last (real
    production evidence: Runs #127-137 executed through this module's loop, Runs
    #139-141 through planning_stage_contract.py's loop). These tests prove that no
    longer matters: once the newer, more complete explicit Stage Contract router
    (PlanningStageError taxonomy, explicit per-stage admission, structural+semantic
    validation before the single durable cache write) is live, install_router() here
    can never silently replace it - regardless of which module installs second."""

    def setUp(self) -> None:
        from scripts import planning_stage_contract as contract

        self._contract = contract
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()
        self._contract_cache_patch = patch.object(
            contract.router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json"
        )
        self._contract_cache_patch.start()
        self._old_json_text = staged.json_text
        self._old_schema_adapter = contract.router._structured_schema_for_prompt
        # Force a fresh contract-router closure so its checkpoint/cooldown state is
        # isolated from any earlier test that already installed it in this interpreter.
        staged.json_text = lambda *_args, **_kwargs: {}

    def tearDown(self) -> None:
        staged.json_text = self._old_json_text
        self._contract.router._structured_schema_for_prompt = self._old_schema_adapter
        self._contract_cache_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_stage_contract_installed_first_survives_install_router_running_after(self) -> None:
        self._contract.install_planning_contract_router()
        contract_router = staged.json_text
        self.assertTrue(getattr(contract_router, router._EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER, False))

        router.install_router()

        self.assertIs(
            staged.json_text,
            contract_router,
            "install_router() must not replace an already-live explicit Stage Contract router",
        )

    def test_stage_contract_installed_first_still_gets_install_routers_other_side_effects(self) -> None:
        """Only the json_text assignment is skipped - checkpoint bootstrap, telemetry
        reset, and the routed_build_plan dialogue_qa wrapper still run unconditionally."""
        self._contract.install_planning_contract_router()

        router._TELEMETRY.append({"provider": "stale", "result": "success"})
        router.install_router()

        self.assertEqual(router.get_telemetry(), [])
        self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))

    def test_install_router_first_then_stage_contract_still_gives_stage_contract_ownership(self) -> None:
        """The normal/documented canonical order (install_router() before
        install_planning_contract_router(), matching planning_runtime_contract.py's
        install_entrypoint_planning_contracts()) already gives the Stage Contract
        ownership; this is the same guarantee from the other direction."""
        router.install_router()
        self.assertFalse(
            getattr(staged.json_text, router._EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER, False)
        )

        self._contract.install_planning_contract_router()

        self.assertTrue(getattr(staged.json_text, router._EXPLICIT_STAGE_CONTRACT_ROUTER_MARKER, False))


if __name__ == "__main__":
    unittest.main()
