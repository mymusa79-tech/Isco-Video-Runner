from __future__ import annotations

import unittest
from unittest import mock

from scripts import task_level_planner_router as router


class ProviderReliabilityRun113Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._original_staged_json_text = router.staged.json_text
        self._original_orchestrator_build_plan = router.orchestrator.build_plan
        self._patches = [
            mock.patch.object(router, "_read_secret_file", return_value="fake-key"),
            mock.patch.object(router, "_load_checkpoint", return_value={"version": 1, "responses": {}}),
            mock.patch.object(router, "_save_checkpoint", return_value=None),
            mock.patch.object(router, "MIN_PROVIDER_CALL_INTERVAL_SECONDS", 0.0),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self) -> None:
        router.staged.json_text = self._original_staged_json_text
        router.orchestrator.build_plan = self._original_orchestrator_build_plan
        for patcher in reversed(self._patches):
            patcher.stop()

    def test_outline_schema_is_strict_and_requires_exact_section_count(self) -> None:
        prompt = """
Required number of sections: exactly 8.
Return ONLY JSON with editorial_intent and section_briefs.
"""
        name, schema = router._structured_schema_for_prompt(prompt)
        self.assertEqual(name, "editorial_outline")
        self.assertFalse(schema["additionalProperties"])
        briefs = schema["properties"]["section_briefs"]
        self.assertEqual(briefs["minItems"], 8)
        self.assertEqual(briefs["maxItems"], 8)
        self.assertFalse(briefs["items"]["additionalProperties"])
        self.assertIn("editorial_intent", schema["required"])
        self.assertIn("section_briefs", schema["required"])

    def test_full_script_schema_requires_exact_entries(self) -> None:
        prompt = (
            'Return ONLY JSON: {"sections": [{"id": "...", "narration": "...", '
            '"key_point": "..."}, ...]} with EXACTLY 8 entries'
        )
        name, schema = router._structured_schema_for_prompt(prompt)
        self.assertEqual(name, "full_script")
        sections = schema["properties"]["sections"]
        self.assertEqual(sections["minItems"], 8)
        self.assertEqual(sections["maxItems"], 8)
        self.assertEqual(
            sections["items"]["required"],
            ["id", "narration", "key_point"],
        )

    def test_openrouter_known_contract_delegates_free_model_fallback_and_schema(self) -> None:
        captured: dict = {}
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }

        def fake_openrouter(prompt, **kwargs):
            captured["prompt"] = prompt
            captured.update(kwargs)
            return {"ok": True}

        with mock.patch.object(router, "openrouter_json_text", side_effect=fake_openrouter):
            result = router._openrouter_call_with_repair(
                "structured prompt",
                "openrouter/free",
                response_contract=("test_contract", schema),
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(captured["model"], "openrouter/free")
        self.assertEqual(
            captured["fallback_models"],
            ("openai/gpt-oss-20b:free",),
        )
        self.assertEqual(captured["response_schema"], schema)
        self.assertEqual(captured["schema_name"], "test_contract")

    def test_unknown_json_contract_keeps_legacy_two_argument_adapter_compatible(self) -> None:
        calls: list[tuple[str, str]] = []

        def legacy_openrouter(prompt, model):
            calls.append((prompt, model))
            return {"ok": True}

        with mock.patch.object(router, "openrouter_json_text", side_effect=legacy_openrouter):
            result = router._openrouter_call_with_repair("legacy prompt", "openrouter/free")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [("legacy prompt", "openrouter/free")])

    def test_transient_503_gets_one_bounded_retry_then_succeeds(self) -> None:
        gemini_calls = 0

        def flaky_gemini(*args, **kwargs):
            nonlocal gemini_calls
            del args, kwargs
            gemini_calls += 1
            if gemini_calls == 1:
                raise RuntimeError("HTTP 503 server error")
            return {"ok": True}

        with mock.patch.object(router, "gemini_json_text", side_effect=flaky_gemini), \
                mock.patch.object(router.time, "sleep") as sleep:
            router.install_router()
            result = router.staged.json_text("ignored", "generic planning task")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(gemini_calls, 2)
        self.assertEqual(
            [entry["result"] for entry in router.get_telemetry()],
            ["server_error", "success"],
        )
        self.assertTrue(sleep.called)
        self.assertGreaterEqual(sleep.call_args_list[0].args[0], router.TRANSIENT_RETRY_BASE_SECONDS)

    def test_429_is_not_retried_and_fails_over_immediately(self) -> None:
        gemini_calls = 0
        groq_calls = 0

        def limited_gemini(*args, **kwargs):
            nonlocal gemini_calls
            del args, kwargs
            gemini_calls += 1
            raise RuntimeError("HTTP 429 rate limited")

        def healthy_groq(prompt):
            nonlocal groq_calls
            del prompt
            groq_calls += 1
            return {"ok": True}

        with mock.patch.object(router, "gemini_json_text", side_effect=limited_gemini), \
                mock.patch.object(router, "_groq_call", side_effect=healthy_groq), \
                mock.patch.object(router.time, "sleep"):
            router.install_router()
            result = router.staged.json_text("ignored", "generic planning task")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(gemini_calls, 1)
        self.assertEqual(groq_calls, 1)
        self.assertEqual(
            [entry["result"] for entry in router.get_telemetry()],
            ["429", "success"],
        )


if __name__ == "__main__":
    unittest.main()
