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
