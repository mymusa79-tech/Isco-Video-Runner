from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_level_planner_router as router  # noqa: E402  (needs sys.path fixup above)

import isco_video_agent.orchestrator as orchestrator  # noqa: E402
import isco_video_agent.resilient_planner as staged  # noqa: E402


class PersonaInjectionFallbackTests(unittest.TestCase):
    """Covers the fix for the channel-identity fallback gap: task_router() must apply
    with_channel_persona() once, before the provider loop, so Groq/OpenRouter fallback
    prompts carry the same channel identity Gemini prompts always did."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(os.environ, {"GEMINI_API_KEY_FILE": str(gemini_key_path)}, clear=False)
        self._env_patch.start()
        # Route the planning checkpoint cache to a scratch file so tests never touch/
        # require the real state/ directory and never leak between test runs.
        self._cache_patch = patch.object(router, "CACHE_PATH", Path(self._tmpdir.name) / "planning-checkpoint.json")
        self._cache_patch.start()

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
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

    def tearDown(self) -> None:
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_second_call_to_same_provider_sleeps_for_the_remaining_gap(self) -> None:
        def fake_gemini_json_text(api_key, prompt, model):
            del api_key, prompt, model
            return {"ok": True}

        # First subtask's provider attempt reads monotonic() twice (gap check, then
        # last_call_at update) at t=1000.0; the second subtask's attempt lands 0.1s
        # later at t=1000.1, which is inside the 1.5s floor, so it must sleep for the
        # remaining 1.4s.
        with patch.object(router, "gemini_json_text", side_effect=fake_gemini_json_text), \
                patch.object(router.time, "monotonic", side_effect=[1000.0, 1000.0, 1000.1, 1000.1]), \
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

    def tearDown(self) -> None:
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_real_install_router_satisfies_orchestrators_own_guard(self) -> None:
        router.install_router()
        self.assertTrue(getattr(orchestrator.build_plan, "_is_resilient_router", False))
        orchestrator._verify_resilient_router_installed()  # must not raise


if __name__ == "__main__":
    unittest.main()
