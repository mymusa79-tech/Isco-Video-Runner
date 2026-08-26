from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_level_planner_router as router  # noqa: E402

import isco_video_agent.resilient_planner as staged  # noqa: E402


class GroqPayloadTooLargeRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        gemini_key_path = Path(self._tmpdir.name) / "gemini_key"
        gemini_key_path.write_text("fake-gemini-key", encoding="utf-8")
        self._env_patch = patch.dict(
            os.environ,
            {"GEMINI_API_KEY_FILE": str(gemini_key_path)},
            clear=False,
        )
        self._env_patch.start()
        self._cache_patch = patch.object(
            router,
            "CACHE_PATH",
            Path(self._tmpdir.name) / "planning-checkpoint.json",
        )
        self._cache_patch.start()
        self._sleep_patch = patch.object(router.time, "sleep")
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()
        self._cache_patch.stop()
        self._env_patch.stop()
        self._tmpdir.cleanup()

    @staticmethod
    def _gemini_rate_limited(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("HTTP 429 rate limited")

    def test_groq_413_fails_over_immediately_to_openrouter(self) -> None:
        groq_calls = 0
        openrouter_calls = 0

        def fake_groq(prompt):
            nonlocal groq_calls
            del prompt
            groq_calls += 1
            raise RuntimeError("Groq HTTP 413")

        def fake_openrouter(prompt, model):
            nonlocal openrouter_calls
            del prompt, model
            openrouter_calls += 1
            return {"ok": True}

        with patch.object(router, "gemini_json_text", side_effect=self._gemini_rate_limited), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            router.install_router()
            result = staged.json_text(
                "unused-api-key",
                "نداء اليقظة: اختبار تحويل 413",
                model="gemini-2.5-flash",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(groq_calls, 1)
        self.assertEqual(openrouter_calls, 1)
        telemetry = router.get_telemetry()
        self.assertEqual([x["provider"] for x in telemetry[:3]], [
            "gemini",
            "groq",
            "openrouter",
        ])
        self.assertEqual(telemetry[1]["result"], "payload_too_large")
        self.assertEqual(telemetry[1]["error_detail"], "Groq HTTP 413")

    def test_groq_remains_eligible_for_later_smaller_repair_request(self) -> None:
        groq_calls = 0
        openrouter_calls = 0

        def fake_groq(prompt):
            nonlocal groq_calls
            groq_calls += 1
            if "oversized-parent-request" in prompt:
                raise RuntimeError("Groq HTTP 413")
            return {"ok": True, "provider": "groq"}

        def fake_openrouter(prompt, model):
            nonlocal openrouter_calls
            del prompt, model
            openrouter_calls += 1
            return {"ok": True, "provider": "openrouter"}

        with patch.object(router, "gemini_json_text", side_effect=self._gemini_rate_limited), \
                patch.object(router, "_groq_call", side_effect=fake_groq), \
                patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            router.install_router()
            first = staged.json_text(
                "unused-api-key",
                "نداء اليقظة oversized-parent-request",
                model="gemini-2.5-flash",
            )
            second = staged.json_text(
                "unused-api-key",
                "نداء اليقظة compact-repair-request",
                model="gemini-2.5-flash",
            )

        self.assertEqual(first["provider"], "openrouter")
        self.assertEqual(second["provider"], "groq")
        self.assertEqual(groq_calls, 2)
        self.assertEqual(openrouter_calls, 1)

        groq_events = [x for x in router.get_telemetry() if x["provider"] == "groq"]
        self.assertEqual(len(groq_events), 2)
        self.assertEqual(groq_events[0]["result"], "payload_too_large")
        self.assertEqual(groq_events[1]["result"], "success")


if __name__ == "__main__":
    unittest.main()
