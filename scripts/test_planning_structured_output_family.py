from __future__ import annotations

import importlib
import unittest
from unittest import mock

from scripts import planning_structured_output_family as family


class PlanningStructuredOutputFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        family._GEMINI_OUTLINE_TRUNCATIONS.clear()

    def test_outline_prompt_compaction_is_idempotent(self) -> None:
        first = family._compact_outline_prompt("PROMPT")
        second = family._compact_outline_prompt(first)
        self.assertEqual(first, second)
        self.assertIn(family._OUTLINE_COMPACTION_MARKER, first)

    def test_groq_schema_failure_classifier_is_narrow(self) -> None:
        self.assertTrue(
            family._is_groq_schema_generation_failure(
                "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
            )
        )
        self.assertTrue(
            family._is_groq_schema_generation_failure(
                "structured_generation_failed"
            )
        )
        self.assertFalse(
            family._is_groq_schema_generation_failure(
                "GROQ_HTTP_429 quota exhausted"
            )
        )

    def test_gemini_outline_uses_provider_specific_budget_then_bounded_recovery(self) -> None:
        module = importlib.reload(family)
        calls: list[dict] = []

        def fake_gemini(api_key, prompt, model="model", *, max_output_tokens=None, **kwargs):
            calls.append(
                {
                    "prompt": prompt,
                    "max_output_tokens": max_output_tokens,
                }
            )
            if len(calls) == 1:
                raise RuntimeError("GEMINI_INTERACTION_OUTPUT_TRUNCATED")
            return {"ok": True}

        original_gemini = module.router.gemini_json_text
        original_groq = module.router._groq_call
        original_unavailable = module.run125._is_model_unavailable
        try:
            module.router.gemini_json_text = fake_gemini
            module.router._groq_call = lambda prompt: {"ok": True}
            module.run125._is_model_unavailable = lambda error: False
            module._certify_transport_composition = lambda: None
            module.router._CURRENT_REQUEST_META.clear()
            module.router._CURRENT_REQUEST_META.update(
                {
                    "response_contract": "editorial_outline",
                    "input_hash": "request-1",
                }
            )
            module.install_planning_structured_output_family()

            with self.assertRaisesRegex(RuntimeError, "OUTPUT_TRUNCATED"):
                module.router.gemini_json_text("key", "PROMPT", model="model")
            result = module.router.gemini_json_text("key", "PROMPT", model="model")

            self.assertEqual(result, {"ok": True})
            self.assertEqual(calls[0]["max_output_tokens"], 4096)
            self.assertEqual(calls[1]["max_output_tokens"], 6144)
            self.assertIn(module._OUTLINE_COMPACTION_MARKER, calls[0]["prompt"])
            self.assertIn(module._OUTLINE_COMPACTION_MARKER, calls[1]["prompt"])
        finally:
            module.router.gemini_json_text = original_gemini
            module.router._groq_call = original_groq
            module.run125._is_model_unavailable = original_unavailable
            module._INSTALLED = False
            module.router._CURRENT_REQUEST_META.clear()

    def test_install_extends_groq_model_failover_only_for_outline_schema_failure(self) -> None:
        module = importlib.reload(family)
        original_gemini = module.router.gemini_json_text
        original_groq = module.router._groq_call
        original_unavailable = module.run125._is_model_unavailable
        try:
            def fake_gemini(api_key, prompt, model="model", *, max_output_tokens=None, **kwargs):
                return {"ok": True}

            module.router.gemini_json_text = fake_gemini
            module.router._groq_call = lambda prompt: {"ok": True}
            module.run125._is_model_unavailable = lambda error: False
            module._certify_transport_composition = lambda: None
            module.router._CURRENT_REQUEST_META.clear()
            module.router._CURRENT_REQUEST_META.update(
                {"response_contract": "editorial_outline", "input_hash": "request-2"}
            )
            module.install_planning_structured_output_family()

            self.assertTrue(
                module.run125._is_model_unavailable(
                    RuntimeError(
                        "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
                    )
                )
            )
            self.assertFalse(
                module.run125._is_model_unavailable(RuntimeError("GROQ_HTTP_500"))
            )

            module.router._CURRENT_REQUEST_META["response_contract"] = "full_script"
            self.assertFalse(
                module.run125._is_model_unavailable(
                    RuntimeError(
                        "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
                    )
                )
            )
        finally:
            module.router.gemini_json_text = original_gemini
            module.router._groq_call = original_groq
            module.run125._is_model_unavailable = original_unavailable
            module._INSTALLED = False
            module.router._CURRENT_REQUEST_META.clear()

    def test_transport_canary_requires_strict_groq_outline_schema(self) -> None:
        module = importlib.reload(family)
        fake_gemini = mock.Mock()
        fake_gemini.__signature__ = None
        # The real composition is certified in the full runtime tests. This unit test
        # verifies the strict-schema half of the canary without replacing production
        # provider behavior.
        with mock.patch.object(
            module.capacity,
            "_response_format_for_contract",
            return_value={
                "type": "json_schema",
                "json_schema": {"strict": False, "schema": {}},
            },
        ):
            with self.assertRaisesRegex(
                RuntimeError, "groq_outline_strict_schema_disabled"
            ):
                # Bypass only the signature check so this assertion isolates strictness.
                with mock.patch.object(module.inspect, "signature") as signature:
                    signature.return_value.bind.return_value = None
                    module._certify_transport_composition()


if __name__ == "__main__":
    unittest.main()
