from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from scripts import planning_batch_hardening as batching
from scripts import provider_capacity_hardening as capacity
from scripts import task_level_planner_router as router
from scripts.provider_failure import classify_provider_failure


class _Response:
    def __init__(self, body: dict, *, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self.headers = {}

    def json(self):
        return self._body


FULL_SCRIPT_PROMPT = '''
Write a bounded batch.
Return ONLY JSON: {"sections": [{"id":"s1","narration":"...","key_point":"..."}]}
with EXACTLY 3 entries, in this exact order.
'''


class Run118BatchHeadroomTests(unittest.TestCase):
    def test_film_transport_is_three_three_two(self) -> None:
        self.assertEqual(batching.MAX_SCRIPT_BATCH_SECTIONS, 3)
        chunks = [part for _start, part in batching._chunks(list(range(8)))]
        self.assertEqual([len(part) for part in chunks], [3, 3, 2])

    def test_story_transport_is_three_two(self) -> None:
        chunks = [part for _start, part in batching._chunks(list(range(5)))]
        self.assertEqual([len(part) for part in chunks], [3, 2])


class Run118GroqGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()

    def test_output_heavy_groq_uses_json_object_and_low_reasoning(self) -> None:
        captured = {}
        content = json.dumps({
            "sections": [
                {"id": "s1", "narration": "أ", "key_point": "١"},
                {"id": "s2", "narration": "ب", "key_point": "٢"},
                {"id": "s3", "narration": "ج", "key_point": "٣"},
            ]
        }, ensure_ascii=False)

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _Response({
                "model": "openai/gpt-oss-20b",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            })

        with patch.object(router, "_read_secret_file", return_value="test-key"), patch.object(
            router.requests, "post", side_effect=fake_post
        ):
            result = capacity._hardened_groq_call(FULL_SCRIPT_PROMPT)

        self.assertEqual(len(result["sections"]), 3)
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning_effort"], "low")
        self.assertFalse(captured["include_reasoning"])
        self.assertEqual(captured["max_completion_tokens"], 2400)

    def test_outline_keeps_strict_schema(self) -> None:
        prompt = "Required number of sections: exactly 8. Return section_briefs."
        contract = router._structured_schema_for_prompt(prompt)
        self.assertIsNotNone(contract)
        response_format = capacity._response_format_for_contract(contract)
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])

    def test_json_validate_failed_is_request_scoped_generation_error(self) -> None:
        failure = classify_provider_failure(
            "groq",
            RuntimeError(
                "GROQ_JSON_VALIDATE_FAILED status=400 code=json_validate_failed"
            ),
        )
        self.assertEqual(failure.telemetry_result, "generation_error")
        self.assertFalse(failure.open_circuit)


class Run119OpenRouterFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        router._last_call_rate_limit_headers.clear()
        router._last_call_response_meta.clear()

    def test_output_heavy_openrouter_uses_dynamic_free_router(self) -> None:
        captured = {}
        contract = router._structured_schema_for_prompt(FULL_SCRIPT_PROMPT)
        self.assertIsNotNone(contract)
        content = json.dumps({
            "sections": [
                {"id": "s1", "narration": "أ", "key_point": "١"},
                {"id": "s2", "narration": "ب", "key_point": "٢"},
                {"id": "s3", "narration": "ج", "key_point": "٣"},
            ]
        }, ensure_ascii=False)

        def fake_post(url, *, headers, json, timeout):
            captured.update(json)
            return _Response({
                "model": "some/current-free-model",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 200},
            })

        with patch.object(router, "_openrouter_key", return_value="test-key"), patch.object(
            router.requests, "post", side_effect=fake_post
        ):
            result = capacity._hardened_openrouter_structured_request(FULL_SCRIPT_PROMPT, contract)

        self.assertEqual(len(result["sections"]), 3)
        self.assertEqual(capacity.OPENROUTER_OUTPUT_HEAVY_MODEL, "openrouter/free")
        self.assertEqual(captured["models"], ["openrouter/free"])
        self.assertNotIn("openai/gpt-oss-20b:free", captured["models"])
        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["reasoning"], {"effort": "low", "exclude": True})
        self.assertEqual(captured["max_tokens"], 2400)
        self.assertTrue(captured["provider"]["allow_fallbacks"])
        self.assertTrue(captured["provider"]["require_parameters"])


if __name__ == "__main__":
    unittest.main()
